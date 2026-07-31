# -*- coding: utf-8 -*-
"""PRD-016 教务速办机器人 · 意图与槽位抽取（SOP 路由层）。

🔴 设计铁律（D3）：本模块**纯规则 + 正则，零 I/O、零 LLM**。
LLM 只在 executor 里给「反馈文案」用，且产出只进反馈单五列字段（walled）。
动作集之外一律 HELP（回能力清单），绝不放任模型自由发挥。

学生名不在这里正则——花名册是外部事实，由调用方把 roster 名单传进来做模糊匹配，
本模块只负责「在文本里找出哪些花名册名字出现了」，保持无副作用可单测。
"""

import re

# ─────────────────────────── 意图常量 ───────────────────────────

HELP = "help"                       # 能力清单（兜底）
ACCOUNT_OPEN = "account_open"       # ① 开户 / 改单价（可带「顺手充值」）
ACCOUNT_RECHARGE = "account_recharge"  # ① 充值
ACCOUNT_ADJUST = "account_adjust"   # ① 调整（课时/金额增减）
SETTLE_PENDING = "settle_pending"   # ② 查待办（只读）
SETTLE_DO = "settle_do"             # ② 一键结算（写）
LEAVE = "leave"                     # ③ 请假冲正（写）
FEEDBACK_TEXT = "feedback_text"     # ④ 口述要点写反馈（写）
FEEDBACK_IMAGE = "feedback_image"   # ⑥ 作业图 → 多模态建反馈单（写）
LEDGER = "ledger"                   # ⑤ 查台账 / 流水 / 余额（只读）
FAMILY_LEDGER = "family_ledger"     # ⑦ 家庭合并余额（只读）
FAMILY_RECHARGE = "family_recharge"  # ⑦ 家庭充值 → 全额进钱包户（写）
FAMILY_REBALANCE = "family_rebalance"  # ⑦ 归账对倒（写）
CONFIRM = "confirm"                 # 确认闸
CANCEL = "cancel"                   # 取消
CHOICE = "choice"                   # 歧义消解：回一个序号

WRITE_INTENTS = {
    ACCOUNT_OPEN, ACCOUNT_RECHARGE, ACCOUNT_ADJUST,
    SETTLE_DO, LEAVE, FEEDBACK_TEXT, FEEDBACK_IMAGE,
    FAMILY_RECHARGE, FAMILY_REBALANCE,
}

CAPABILITY_LIST = """我只做教务速办这六件事（说人话就行）：

① 账户｜开户 / 充值 / 调整
   · 给俊羽开个数学户 350 一节
   · 俊羽充 20 节
   · 俊羽调整 -1 节 备注 补扣
② 结算｜查待办 / 一键结算
   · 有几场待结算
   · 把待办清了（可加：按 1.5 课时扣 / 只结俊羽的）
③ 请假冲正
   · 俊羽 7月26 请假
④ 写反馈（口述要点）
   · 帮我写今天俊羽的反馈：讲了周期问题，掌握不错；三位数乘两位数还要巩固
⑤ 查台账 / 流水 / 余额
   · 俊羽台账      · 俊羽还剩多少课时
⑥ 发作业图 → 自动整理反馈单
   · 直接发图（可多张），发完说一句「生成反馈」
⑦ 家庭钱包（好好 + 俊羽一家）
   · 他们家还剩多少     —— 两户金额相加的合计
   · 他们家充 7000      —— 全额充进家庭钱包户（俊羽户）
   · 归账              —— 好好户欠的钱从俊羽户对倒平掉

🔴 涉及钱和落库的操作我都会先回一条「预览」，你回「确认」我才真执行（10 分钟内有效）。
其它需求我不支持——细改请上 H5：http://jpjia.cn/#/m/schedule"""

# ─────────────────────────── 词表 ───────────────────────────

_CONFIRM_WORDS = {"确认", "确定", "确认执行", "执行", "对", "是", "好", "可以", "ok", "OK", "yes", "Y", "y", "嗯"}
_CANCEL_WORDS = {"取消", "算了", "不用了", "不了", "撤销", "cancel", "no", "N", "n"}

_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

SUBJECTS = {"数学": "1", "科学": "2", "语文": "3", "英语": "4"}

# ─────────────────────────── 基础解析件 ───────────────────────────

_NUM = r"(?:\d+(?:\.\d+)?|[零一两二三四五六七八九十]+半?|半)"


def _to_num(s):
    """「1.5」「两」「十」「一节半」→ float；解析不出返 None。"""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    half = s.endswith("半")
    body = s[:-1] if half else s
    val = 0.0
    if body == "":
        val = 0.0
    elif body == "十":
        val = 10.0
    elif body.startswith("十"):          # 十五
        val = 10.0 + _CN_NUM.get(body[1:], 0)
    elif body.endswith("十"):            # 二十
        val = _CN_NUM.get(body[:-1], 0) * 10.0
    elif "十" in body:                   # 二十五
        a, b = body.split("十", 1)
        val = _CN_NUM.get(a, 1) * 10.0 + _CN_NUM.get(b, 0)
    elif body in _CN_NUM:
        val = float(_CN_NUM[body])
    else:
        return None
    return val + (0.5 if half else 0.0)


_PRICE_EXPR = re.compile(r"\d+(?:\.\d+)?\s*元?\s*(?:一节|每节|／节|/节|一课时|每课时)")


def parse_hours(text):
    """「20 节」「1.5 课时」「两次」→ float。带正负号则保留符号（调整场景）。

    🔴 先剥掉单价表达式——「350 一节充 20 节」里的「一节」是单价的量词不是课时数，
    不剥会把 20 节读成 1 节（自测抓到的真 bug）。
    """
    text = _PRICE_EXPR.sub(" ", text)
    m = re.search(r"([+\-＋－]?)\s*(" + _NUM + r")\s*(?:节课|节|课时|次)", text)
    if not m:
        return None
    v = _to_num(m.group(2))
    if v is None:
        return None
    return -v if m.group(1) in ("-", "－") else v


def parse_amount(text):
    """「7000 元」「充 7000」→ float（元）。"""
    m = re.search(r"([+\-＋－]?)\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块|￥)", text)
    if m:
        v = float(m.group(2))
        return -v if m.group(1) in ("-", "－") else v
    m = re.search(r"(?:充值?|交(?:了|费)?|收(?:了)?)\s*(\d{3,})(?!\s*(?:节|课时|次|元))", text)
    return float(m.group(1)) if m else None


def parse_price(text):
    """「350 一节」「每节 350」「单价 350」→ float。"""
    for pat in (r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:一节|每节|／节|/节|一课时|每课时)",
                r"(?:单价|价格|按)\s*(\d+(?:\.\d+)?)\s*(?:元)?"):
        m = re.search(pat, text)
        if m:
            return float(m.group(1))
    return None


def parse_settle_hours(text):
    """②「按 1.5 课时扣」「每场扣 2 节」→ 本次结算实扣课时。"""
    m = re.search(r"(?:按|每场|一场|各)\s*(" + _NUM + r")\s*(?:节|课时)", text)
    if m:
        return _to_num(m.group(1))
    m = re.search(r"扣\s*(" + _NUM + r")\s*(?:节|课时)", text)
    return _to_num(m.group(1)) if m else None


def parse_time_note(text):
    """「09:05-10:40」→ 实际上课时间备注。"""
    m = re.search(r"(\d{1,2}[:：]\d{2})\s*[-–~～至到]\s*(\d{1,2}[:：]\d{2})", text)
    if not m:
        return None
    return "%s-%s" % (m.group(1).replace("：", ":"), m.group(2).replace("：", ":"))


def parse_date(text, today):
    """「2026-07-26」「7月26」「7/26」「今天/昨天/前天/明天」→ 'YYYY-MM-DD'；无则 None。

    today = datetime.date 对象（由调用方注入，保持本模块无 I/O 可测）。
    """
    import datetime
    rel = {"今天": 0, "今日": 0, "本日": 0, "昨天": -1, "昨日": -1, "前天": -2, "明天": 1, "明日": 1}
    for k, off in rel.items():
        if k in text:
            return (today + datetime.timedelta(days=off)).isoformat()
    # 时间段「09:05-10:40」里的「05-10」不是日期，先剥掉
    text = re.sub(r"\d{1,2}[:：]\d{2}\s*[-–~～至到]\s*\d{1,2}[:：]\d{2}", " ", text)
    m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # 🔴 短日期分隔符不含「.」——否则「按 1.5 课时扣」会被读成 1 月 5 日（自测抓到的真 bug）
    m = re.search(r"(?<!\d)(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*(?:日|号)?(?!\d)", text)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            return "%04d-%02d-%02d" % (today.year, mo, da)
    return None


def parse_subject(text):
    for label in SUBJECTS:
        if label in text:
            return label
    return None


def parse_session_id(text):
    """「场次 323」「#323」「结 323」→ 场次 id（纯数字串）。"""
    m = re.search(r"(?:场次|场|课次|session|#)\s*#?\s*(\d{2,})", text)
    return m.group(1) if m else None


def parse_note(text):
    """「备注 补扣上月」→ note。"""
    m = re.search(r"(?:备注|说明|原因)[:：]?\s*(.+)$", text)
    return m.group(1).strip() if m else None


def match_students(text, roster):
    """在文本里找花名册命中项。roster = [{'id','name',...}]。

    三级：① 全名出现 → 精确命中（唯一即定）；② 名字任一 2 字连续片段出现；
    ③ 都没有 → 返回空（由调用方追问）。返回命中的 roster 行列表（去重保序）。
    """
    hits, seen = [], set()
    for r in roster:
        name = (r.get("name") or "").strip()
        if name and name in text and r.get("id") not in seen:
            seen.add(r.get("id"))
            hits.append(r)
    if hits:
        return hits
    for r in roster:
        name = (r.get("name") or "").strip()
        if len(name) < 2 or r.get("id") in seen:
            continue
        for i in range(len(name) - 1):
            if name[i:i + 2] in text:
                seen.add(r.get("id"))
                hits.append(r)
                break
    return hits


# ─────────────────────────── 意图判定 ───────────────────────────

_RE_HELP = re.compile(r"帮助|能做(什么|啥)|你会(什么|啥)|功能|菜单|怎么用|help|指令|命令列表")
_RE_FEEDBACK_IMG = re.compile(r"生成反馈|整理反馈|看图|读图|按图|图.{0,4}反馈|开始整理")
_RE_FEEDBACK = re.compile(r"反馈单|写反馈|记反馈|反馈")
_RE_LEAVE = re.compile(r"请假|冲正|销课|退课时|课取消|取消(这|那)?(节|场)")
# 🔴「待结算」里的「结算」是名词不是命令 → 负向后顾把它挡在只读侧（自测抓到的真 bug）
_RE_SETTLE_DO = re.compile(r"(?<!待)结算|结了|结掉|清了|清掉|都结|全结|结一下|结账|只结|单结")
_RE_SETTLE_PENDING = re.compile(r"待结算|待办|没结|未结|几场|哪些课")
_RE_OPEN = re.compile(r"开\S{0,3}户|建\S{0,2}户|新开|开个账|改单价|调单价|改价")
_RE_RECHARGE = re.compile(r"充值|充\s*\d|充[了一两三四五六七八九十]|续费|交费|交了|续课")
_RE_ADJUST = re.compile(r"调整|补\s*\d|补[了一两三四五六七八九十]|多扣|少扣|退还|修正")
_RE_LEDGER = re.compile(r"台账|流水|余额|还剩|剩多少|剩几|账户|对账|消耗")
# ⑦ 家庭钱包（写死好好+俊羽一家，不做通用配置）
_RE_FAMILY = re.compile(r"他们家|她们家|俩家|一家|全家|家庭|家里|好好俊羽|俊羽好好|两个孩子|俩孩子|两个娃")
_RE_REBALANCE = re.compile(r"归账|归拢|对倒|平账|轧账|抹平")
_RE_MONEYQ = re.compile(r"多少钱|多少课时|几节|共多少|一共")


def detect(text, has_images=False):
    """→ 意图常量。纯文本判定；has_images 只影响⑥的触发。"""
    t = (text or "").strip()
    if not t:
        return HELP
    if t in _CONFIRM_WORDS:
        return CONFIRM
    if t in _CANCEL_WORDS:
        return CANCEL
    if re.fullmatch(r"[1-9]\d?", t):
        return CHOICE
    if _RE_HELP.search(t):
        return HELP
    # ⑦ 家庭钱包先判：「归账」独立触发；家庭词只有配上钱/余额问法才算家庭意图，
    #    否则（如「他们家请假」）落回常规单人链路。
    if _RE_REBALANCE.search(t):
        return FAMILY_REBALANCE
    if _RE_FAMILY.search(t):
        if _RE_RECHARGE.search(t):
            return FAMILY_RECHARGE
        if _RE_LEDGER.search(t) or _RE_MONEYQ.search(t):
            return FAMILY_LEDGER
    # ⑥ 优先于④：手上有图 + 说了「生成反馈」= 走多模态；没图也说了 → 提示先发图
    if _RE_FEEDBACK_IMG.search(t):
        return FEEDBACK_IMAGE
    if _RE_LEAVE.search(t):
        return LEAVE
    # 「把待办清了」= 执行；「有几场待结算」= 只读。先判执行词。
    if _RE_SETTLE_DO.search(t):
        return SETTLE_DO
    if _RE_SETTLE_PENDING.search(t):
        return SETTLE_PENDING
    if _RE_FEEDBACK.search(t):
        return FEEDBACK_IMAGE if has_images else FEEDBACK_TEXT
    if _RE_OPEN.search(t):
        return ACCOUNT_OPEN
    if _RE_RECHARGE.search(t):
        return ACCOUNT_RECHARGE
    if _RE_ADJUST.search(t):
        return ACCOUNT_ADJUST
    if _RE_LEDGER.search(t):
        return LEDGER
    return HELP


def extract(intent, text, roster, today):
    """按意图抽槽位 → dict（不含任何 I/O 结果；id 一律字符串）。"""
    t = text or ""
    s = {
        "students": match_students(t, roster),
        "subject": parse_subject(t),
        "date": parse_date(t, today),
        "note": parse_note(t),
        "raw": t,
    }
    if intent in (ACCOUNT_OPEN, ACCOUNT_RECHARGE, ACCOUNT_ADJUST, FAMILY_RECHARGE):
        s["price"] = parse_price(t)
        s["hours"] = parse_hours(t)
        s["amount"] = parse_amount(t)
    elif intent == SETTLE_DO:
        s["hours"] = parse_settle_hours(t)
        s["timeNote"] = parse_time_note(t)
        s["sessionId"] = parse_session_id(t)
    elif intent == LEAVE:
        s["sessionId"] = parse_session_id(t)
    return s
