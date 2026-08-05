# -*- coding: utf-8 -*-
"""PRD-016 教务速办机器人 · 意图常量 + 槽位解析件。

🔴 2026-08-01 角色转变（换脑，务必看清，别把它当死代码删了）：
    本模块**曾经是路由器**（一张 `_RE_*` 关键词表判 intent）。那张表已经拆掉了——
    它没有「我不懂」这个出口，不认识的诉求会被表里最近的关键词抢走，于是装懂给个错
    东西（老师说「重新记录课程**消耗**情况」被「消耗」二字判成查台账，是真实事故）。

    现在：**判 intent 的活全部归 brain.py（LLM）**。本模块只剩两个角色——
      ① 常量表（意图枚举 / 学科码 / 能力清单）；
      ② **校验器 + 兜底解析器**：parse_hours / parse_date / match_students 这一票函数
         不再决定意图，只负责「LLM 没给出某个槽位时再捞一次」和格式复校。
      ③ 唯一保留的直判 = 确认 / 取消 / 纯数字序号（零歧义、要秒回，不该等 LLM 往返）。

    ❌ 绝不要因为「LLM 挂了」就把关键词路由加回来当降级——那正是翻车的老路。
       理解层跑不通就诚实说没听懂（见 brain.DEGRADED_REPLY）。

本模块仍然保持**零 I/O、零 LLM、无副作用可单测**：学生名靠调用方把 roster 传进来匹配。
"""

import re

# ─────────────────────────── 意图常量 ───────────────────────────

HELP = "help"                       # 能力清单（兜底）
UNKNOWN = "unknown"                 # 🔴 没听懂——新增的「我不懂」出口，绝不滑向最近的关键词
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
FAMILY_REBALANCE = "family_rebalance"  # ⑦ 归账对倒（🔴 PRD-018 退役，executor 回提示语；常量保留只为收住老话术）
INGEST_LESSON_LOG = "ingest_lesson_log"  # ⑧ 看手写课时本照片 → 补录历史场次 + 结算 + 充值（写）
CONFIRM = "confirm"                 # 确认闸
CANCEL = "cancel"                   # 取消
CHOICE = "choice"                   # 歧义消解：回一个序号

WRITE_INTENTS = {
    ACCOUNT_OPEN, ACCOUNT_RECHARGE, ACCOUNT_ADJUST,
    SETTLE_DO, LEAVE, FEEDBACK_TEXT, FEEDBACK_IMAGE,
    FAMILY_RECHARGE, FAMILY_REBALANCE, INGEST_LESSON_LOG,
}

CAPABILITY_LIST = """我做教务速办这几件事（说人话就行，不用背指令）：

① 账户｜开户 / 充值 / 调整
   · 给俊羽开个数学户 350 一节，一节 1.5 小时
   · 俊羽充 20 节   · 俊羽充 30 小时   · 俊羽交了 7000
   · 俊羽调整 -1 节 备注 补扣
② 结算｜查待办 / 一键结算
   · 有几场待结算
   · 把待办清了（可加：按 1.5 课时扣 / 只结俊羽的）
③ 请假冲正
   · 俊羽 7月26 请假
④ 写反馈（口述要点）
   · 帮我写今天俊羽的反馈：讲了周期问题，掌握不错；三位数乘两位数还要巩固
⑤ 查台账 / 流水 / 余额
   · 俊羽台账      · 俊羽还剩多少（报小时，顺带折成几节）
⑥ 发作业图 → 自动整理反馈单
   · 发学生做的题/板书照片（可多张），再说一句「看图写反馈」
⑦ 家庭钱包（好好 + 俊羽一家）
   · 他们家还剩多少     —— 合计小时（同一本共享账只报一次）
   · 他们家充 7000      —— 全额充进家庭钱包户（俊羽户）
   · 归账              —— 已退役：两个孩子的课时已并成一本共享账，没有对倒这回事
⑧ 看手写课时本 → 补录历史记录
   · 拍纸质课时本发我，再说「这是俊羽的课时记录，帮我补录进系统」
   · 我逐行读给你看 → 你核对 → 确认后建场次 + 扣课时 + 补充值笔

🔴 涉及钱和落库的操作我都会先回一条「预览」，你回「确认」我才真执行（10 分钟内有效）。
🔴 没听懂我会直说「我不会」，不会硬猜一个最像的去做——细改请上 H5：http://jpjia.cn/#/m/schedule"""

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

# 🔴 PRD-018：小时 / 节是**两个单位**，解析件必须分开（见 parse_hours / parse_lessons）。
_HOUR_UNIT = r"\s*(?:个)?\s*(?:小时|钟头|h|H|hr|hrs)"
# 「每节时长」表达式（两种语序）：「一节 1.5 小时」「1.5 小时算一节」。
# 它里面既有「节」又有「小时」，不先剥掉会同时污染 parse_lessons（读出 1 节）
# 和 parse_hours（读出 1.5 小时）——「开个户 350 一节（1.5 小时）」会凭空多出一笔充值。
_HPL_EXPR = re.compile(
    r"(?:一节课?|每节课?|一课时|每课时)\s*(?:是|=|＝|按|为|大约|约)?\s*(?:一个半|" + _NUM + r")" + _HOUR_UNIT
    + r"|(?:一个半|" + _NUM + r")" + _HOUR_UNIT + r"\s*(?:算|当|为|是)?\s*(?:一节课?|每节课?|／节|/节|一课时)")


def _to_hour_num(s):
    """时长专用数字：额外认「一个半」= 1.5（中文数字只补这一个最常说的）。"""
    if s is None:
        return None
    s = s.strip()
    if s in ("一个半", "1个半"):
        return 1.5
    return _to_num(s)


def parse_hours(text):
    """🔴 只认**小时**：「3 小时」「1.5个小时」「2h」「半小时」→ float。带正负号保留符号。

    PRD-018 之前它连「节/课时/次」一起吃（那时账本是节本位）；现在底账是小时本位、
    「节」另有 parse_lessons 走 lessons 档交 BE 换算，两者绝不能再混。
    """
    text = _HPL_EXPR.sub(" ", text or "")
    m = re.search(r"([+\-＋－]?)\s*(一个半|" + _NUM + r")" + _HOUR_UNIT, text)
    if not m:
        return None
    v = _to_hour_num(m.group(2))
    if v is None:
        return None
    return -v if m.group(1) in ("-", "－") else v


def parse_lessons(text):
    """🔴 只认**节**：「20 节」「1.5 课时」「两次」→ float。带正负号保留符号（调整场景）。

    老师嘴里的「节 / 节课 / 次 / 课时」历史上一律是「节」，原样进 lessons 档，
    折成多少小时由 BE 按每节时长算（PRD-018 D9），机器人不替它换算。

    🔴 先剥单价与每节时长表达式——「350 一节充 20 节」里的「一节」是单价的量词，
    不剥会把 20 节读成 1 节（自测抓到的真 bug）。
    """
    text = _PRICE_EXPR.sub(" ", _HPL_EXPR.sub(" ", text or ""))
    m = re.search(r"([+\-＋－]?)\s*(" + _NUM + r")\s*(?:节课|节|课时|次)", text)
    if not m:
        return None
    v = _to_num(m.group(2))
    if v is None:
        return None
    return -v if m.group(1) in ("-", "－") else v


def parse_hours_per_lesson(text):
    """「一节 1.5 小时」「每节1.5小时」「一节课一个半小时」「1.5 小时算一节」→ 1.5。

    = 账本绑定层的 hours_per_lesson（PRD-018 D1 v2.1）。读不出返 None（调用方退默认 1.0
    或沿用账户现值）。中文数字只认「一个半」，其余按数字型识别。
    """
    m = _HPL_EXPR.search(text or "")
    if not m:
        return None
    n = re.search(r"(一个半|" + _NUM + r")" + _HOUR_UNIT, m.group(0))
    v = _to_hour_num(n.group(1)) if n else None
    return v if v and v > 0 else None


def parse_amount(text):
    """「7000 元」「充 7000」→ float（元）。"""
    m = re.search(r"([+\-＋－]?)\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块|￥)", text)
    if m:
        v = float(m.group(2))
        return -v if m.group(1) in ("-", "－") else v
    m = re.search(r"(?:充值?|交(?:了|费)?|收(?:了)?)\s*(\d{3,})(?!\s*(?:节|课时|次|元))", text)
    return float(m.group(1)) if m else None


def parse_price(text):
    """「350 一节」「每节 350」「单价 350」「时薪 260」→ float（老师报的那个数，单位看说法）。

    🔴 单位不在这里判：是「每节的钱」还是「时薪」由调用方看措辞定
    （jiaowu_bot._price_per_hour）。这里只负责把数字捞出来。
    🔴 「按 N」后面跟着 节/课时/次/小时 的一律不算价（「按 1.5 课时扣」是结算量，
       被读成单价 1.5 元会让开户预览出现一个荒唐的价）。
    """
    for pat in (r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:一节|每节|／节|/节|一课时|每课时)",
                r"(?:单价|价格|时薪|按)\s*(?:改成|调成|改为|变成|设为|是|=|＝)?\s*"
                r"(\d+(?:\.\d+)?)(?![\d.])\s*(?:元)?(?!\s*(?:节|课时|次|小时))"):
        m = re.search(pat, text)
        if m:
            return float(m.group(1))
    return None


def parse_settle_hours(text):
    """②「按 1.5 课时扣」「每场扣 2 节」→ 本次结算实扣量。

    🔴 语义不动（PRD-018）：结算入参 `items[].hours` 本来就是**小时**，老师这句话说的
    「1.5 课时」在结算语境里就是 1.5 小时；不给就走 BE 的 plannedHours（=每节时长）。
    """
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


# ─────────────────────────── 直判（唯一保留的规则判定） ───────────────────────────
#
# 🔴 这里只剩「确认 / 取消 / 纯数字序号」三种**单词**。
#    它们零歧义、要秒回（等一次 LLM 往返太蠢），而且是确认闸的命脉——
#    理解层挂了也必须能取消掉一条挂起的写操作。
#    ⚠️ 任何「按关键词猜业务意图」的逻辑都不许再加回这里，那是 2026-07-31 翻车的根因。


def detect_direct(text):
    """→ CONFIRM / CANCEL / CHOICE，或 None（= 交给 brain.understand 去理解）。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in _CONFIRM_WORDS:
        return CONFIRM
    if t in _CANCEL_WORDS:
        return CANCEL
    if re.fullmatch(r"[1-9]\d?", t):
        return CHOICE
    return None


# ─────────────────────────── 兜底槽位解析 ───────────────────────────


def extract(text, roster, today):
    """把一句话里能用正则捞的槽位全捞一遍 → dict（零 I/O，id 一律字符串）。

    🔴 角色（2026-08-01 起）：**兜底解析器，不是路由器**。
    调用方 = brain._backfill —— LLM 已经给出的槽位优先，这里只补 LLM 漏掉的那些。
    所以本函数不再按 intent 分支（那时候是为了省无谓的解析），一律全解析；
    多解析出来的槽位没人用也无害，漏解析才要命。
    """
    t = text or ""
    settle_h = parse_settle_hours(t)
    hours, lessons = parse_hours(t), parse_lessons(t)
    if settle_h is not None:
        # 「按 1.5 课时扣」= 本次结算实扣量（入参口径 = 小时），它不是一笔充值的节数，
        # 认它就不要再往 lessons 里塞一份，免得同一句话被两个档同时读走。
        hours, lessons = settle_h, None
    return {
        "students": match_students(t, roster),
        "subject": parse_subject(t),
        "date": parse_date(t, today),
        "note": parse_note(t),
        "price": parse_price(t),
        "hours": hours,               # 🔴 小时（PRD-018 底账单位）
        "lessons": lessons,           # 🔴 节（交 BE 按每节时长换算）
        "hoursPerLesson": parse_hours_per_lesson(t),
        "amount": parse_amount(t),
        "timeNote": parse_time_note(t),
        "sessionId": parse_session_id(t),
        "raw": t,
    }
