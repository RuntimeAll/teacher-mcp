# -*- coding: utf-8 -*-
"""PRD-016 教务速办机器人 · 理解层（LLM 出结构化意图）。

🔴 2026-08-01 换脑：把「纯正则关键词表路由」整个拆掉，理解层全量交 LLM。

为什么换（真实翻车，2026-07-31 23:44~23:50）：
  老师说「帮我重新记录俊羽这个学生的课程**消耗**情况…你帮我系统持久化一下」，
  旧的 `_RE_LEDGER` 词表里恰好有「消耗」两个字 → 判成「查台账」，吐了 8 条流水。
  他发的手写课时本照片从头到尾没被看过一眼。
  根因不是词表漏了哪个词，是**词表没有「我不懂」这个出口**——不认识的诉求
  会被表里最近的关键词抢走，于是装懂给一个错东西。老师原话：「完全没有脑子了」。

本模块的职责边界（务必守住）：
  ✅ 只产「结构化意图 + 槽位候选」，是一个**翻译器**。
  ❌ 不碰数据、不发请求、不做决定。写库仍然只走 executor 的 run_op，
     且仍然必须过 jiaowu_bot 的「预览 → 确认」闸。LLM 换的是耳朵，不是手。

反幻觉三道闸（下面每一条都有对应实现，改代码别拆）：
  ① **LLM 只准说名字，绝不准说 id**——学生 id 一律由代码拿名字回花名册
     （intents.match_students）解析；查不到就当没识别出学生，走原有追问/选序号链路。
  ② **数值槽位代码复校**——hours/amount/price/date/sessionId 一律 float()/日历/正则
     过一遍，校验不过就当没有（None），绝不带着脏值进预览。
  ③ **意图白名单**——不在 INTENTS 里的一律降成 unknown。

降级口径（🔴 绝不静默回落到旧正则——旧正则正是这次翻车的原因）：
  CLI 起不来 / 超时 / JSON 解析不出 → intent=unknown + degraded=True，
  机器人回一句诚实的话，**不猜、不执行**。
"""

import datetime
import os
import re

import executor as X
import intents as I

# 理解层单独的超时（比反馈文案短得多——路由不读图，只读一句话）
BRAIN_TIMEOUT = int(os.environ.get("BRAIN_TIMEOUT", "120"))
# 读图 pass 要逐张 Read 手写照片，给足时间
READ_TIMEOUT = int(os.environ.get("LESSON_LOG_TIMEOUT", str(X.CLI_TIMEOUT)))

# ─────────────────────────── 意图白名单 ───────────────────────────
# 🔴 多一个字都算 unknown。新增能力必须同时改这里 + intents 常量 + jiaowu_bot 路由表。

INTENTS = (
    I.ACCOUNT_OPEN, I.ACCOUNT_RECHARGE, I.ACCOUNT_ADJUST,
    I.SETTLE_PENDING, I.SETTLE_DO,
    I.LEAVE,
    I.FEEDBACK_TEXT, I.FEEDBACK_IMAGE,
    I.LEDGER,
    I.FAMILY_LEDGER, I.FAMILY_RECHARGE, I.FAMILY_REBALANCE,
    I.INGEST_LESSON_LOG,
    I.HELP, I.UNKNOWN,
)

# 降级时说的那句话（🔴 诚实，不猜、不甩清单）
DEGRADED_REPLY = ("我这会儿没听明白（理解模块没跑通）。换个说法再说一次，"
                  "或者用短指令：待办 / 台账 / 归账 / 帮我写反馈")

# ─────────────────────────── 路由 pass 的 wall ───────────────────────────

ROUTER_SYSTEM = u"""你是「教务速办机器人」的意图理解器。一位教培老师在飞书上跟机器人说话，
你要把他这句话翻译成一个结构化 JSON 意图，交给一套**固定的动作集**去执行。

🔴 最重要的一条，先看这个：
   **拿不准就回 unknown，并在 clarify 里说清你不确定什么。**
   绝不为了给出一个答案而勉强归类——归错类比说不知道糟糕得多。
   （血的教训：老师说「帮我重新记录俊羽的课程消耗情况」，上一版路由看到「消耗」
     两个字就当成「查台账」，吐了一堆流水，他要的其实是把纸质记录补录进系统。）

你的唯一输出 = 一个 JSON 对象。不要解释、不要 markdown 代码块、不要前后缀：
{"intent":"...","slots":{...},"clarify":null}

【intent】只能是下面这 15 个之一，多一个字、少一个字都算错：
  account_open       开户 / 改时薪或每节时长。例：给俊羽开个数学户 350 一节（1.5 小时）/ 把俊羽时薪改成 260
  account_recharge   充值。例：俊羽充 20 节 / 他交了 7000 块
  account_adjust     调整增减（非充值的手工加减）。例：俊羽调整 -1 节 备注 补扣 / 上次多扣了一节退回来
  settle_pending     查待结算清单（只读）。例：有几场待结算 / 待办
  settle_do          执行结算（真扣课时）。例：把待办清了 / 只结俊羽的 / 按 1.5 课时扣
  leave              请假冲正。例：俊羽 7月26 请假 / 那节课取消了
  feedback_text      老师口述本节要点 → 写课后反馈单
  feedback_image     看学生作业/板书照片 → 写课后反馈单（老师手上要有图）
  ledger             查台账 / 流水 / 余额（只读）。例：俊羽还剩多少课时 / 还剩几节 / 俊羽台账
  family_ledger      家庭合并余额（只读，写死好好+俊羽一家）。例：他们家还剩多少
  family_recharge    家庭充值。例：他们家充 7000
  family_rebalance   （已退役，仍收：两户已并一本共享账）例：归账 / 把好好户的负数平掉
  ingest_lesson_log  看**手写课时本 / 纸质上课记录**照片，把历史上课记录补录进系统
                     （建历史场次 + 逐场扣课时 + 补充值笔）。例：
                       · 这是我上课的课时记录，帮我把 4 月 9 号到 6 月 28 号的录进去
                       · 帮我重新记录俊羽的课程消耗情况，我给你我之前登记的信息，你帮我持久化一下
                       · 把这本课时本补到系统里
                     🔴 与 feedback_image 的分界：照片拍的是**课时/考勤台账表**（日期+时间+
                       上课内容+扣几节）= ingest_lesson_log；照片拍的是**学生做的题/作业/板书**
                       = feedback_image。
                     🔴 与 ledger 的分界：ledger 是「**查**系统里已有的」，
                       ingest_lesson_log 是「**把纸上的写进**系统」。出现「记录/录入/补录/
                       持久化/存进系统」这类动词 = 写，不是查。
  help               问机器人会做什么 / 要菜单
  unknown            以上都不是，或者你不确定是哪个

【slots】只填你**从这句话里真的读到**的东西。读不到就别写这个键（或写 null）。
  students   学生名字数组，如 ["俊羽"]。
             🔴 只准写名字，绝不准写 id / 编号 / 数字——写了也会被丢掉。
             🔴 花名册里没有的名字不要编，宁可留空数组，机器人会去问他。
  subject    数学 / 科学 / 语文 / 英语
  date       YYYY-MM-DD。相对日期（今天/昨天/上周五）你按下面给的「今天」自己换算好
  dateFrom   YYYY-MM-DD，一段范围的起点（补录场景常见）
  dateTo     YYYY-MM-DD，一段范围的终点
  hours      小时数，数字（该负就写负数）。🔴 老师说「N 小时」才填这里
  lessons    节数，数字。🔴 老师说「N 节」填这里，别自己折算成小时（系统按每节时长换算）
  amount     金额（元），数字。老师说「交了 N 块」只填这里
  price      时薪（元/小时），数字。🔴 老师说「350 一节」这类按节报价 = 每节的钱，
             也照抄进 price 并把节的时长写进 hoursPerLesson（如果他说了），系统会换算
  hoursPerLesson 每节时长（小时），数字。如「俊羽一节课 1.5 小时」= 1.5
  occurDate  YYYY-MM-DD，充值/调整的业务日期（补录历史充值时=真实交钱日期）
  sessionId  场次号，纯数字字符串
  timeNote   实际上课时间，如 "13:30-15:00"
  note       备注 / 原因
  brief      写反馈时老师口述的要点原文（原样抄，别润色）
  noRecharge true / false。只在 ingest_lesson_log 用：他明说了「只补上课记录、
             不要补充值」「充值我自己充过了」之类 → true；没说就别写这个键

【clarify】intent=unknown 时**必填**：一句话说清你不确定什么、缺什么信息。
  像人说话，别甩清单，别道歉一大段。其它 intent 一律填 null。

【本次不需要调用任何工具】直接看文字判断即可，不要去 Read 任何文件或图片。

🔴 最后再说一遍：不确定 → unknown + clarify。别猜。"""

# ─────────────────────────── 读图 pass 的 wall ───────────────────────────

LESSON_LOG_SYSTEM = u"""你是「手写课时本」识别器。老师拍了纸质上课记录本的照片，
你要把它逐行读成结构化 JSON，供系统补录历史场次和扣课时用。

你的唯一输出 = 一个 JSON 对象。不要解释、不要 markdown 代码块、不要前后缀：
{"records":[{"date":"2026-04-12","start":"13:30","end":"15:00","title":"数字迷、图形面积","lessons":1}],
 "recharges":[{"date":"2026-04-12","lessons":10,"note":"4.12 +10 节"}],
 "notes":"哪几处看不清、你拿不准什么"}

🔴 铁律一：这是**手写体，你不许猜**。任何一个字段看不清就填 null，
   宁可让老师自己补，也绝不要编一个「看起来合理」的值。
   日期或时间读错 = 系统里排错课、扣错钱，比留空糟糕得多。

其余口径：
1. records = 表里每一行「已经上过的课」，一行一条，按表上顺序。
2. date 必须 YYYY-MM-DD。表上通常只写「4.12」这种「月.日」，
   年份按下面给你的「今天」推算（推出来的日期比今天还晚，就算上一年）。
3. start / end = HH:mm 24 小时制，照抄表上的（如 13:30-15:00）。
4. lessons = 那一行扣的**节数**（表上写「1 节」就是 1）；没写或看不清填 null。
   🔴 单位照抄本子：本子写「节」就填 lessons，只有本子明写「小时」时才填 hours。
   一节等于几小时不用你算（系统按这个学生的每节时长自己折）。
5. title = 那一行「上课内容」列的原文，**照抄**，不要润色、不要替他补全成完整句子。
   个别字实在认不出，就把认得出的部分写上，别硬凑。
6. recharges = 表格外面/旁边/箭头标注的**充值**笔，例如「4.12 +10 节」「6.28 +10 节」。
   单位同样照抄：写「节」填 lessons，写「小时」填 hours，写「元/块」填 amount，三选一。
   充值和上课是两回事，绝对不要混进 records。
7. 表上通常还有一列「剩余」（9、8、7…递减）。那是他自己的余额演算，
   **不要**把它当成 lessons，也不要输出它——但你可以拿它自检：
   相邻两行剩余差 1，就说明那一行确实扣了 1 节。
8. 图上没有的东西一律不许出现在输出里。

🔴 铁律二：**一行都不许漏**。漏一行 = 少扣一节课的钱，他对不上账还查不出为什么。
   动笔前先数清楚表格里有几行**写了字的数据行**（空行、表头不算），
   records 的条数必须等于这个数；数完在 notes 里写一句「表里 N 行数据，我读出 N 条」。
   有哪一行整行都认不出，也要**出一条 date 全为 null 的占位记录**并在 notes 里点名，
   让老师知道这里漏了——绝不要悄悄跳过。

🔴 工具与产出的关系（别搞反）：
- 「只输出 JSON」约束的是你的**最终回答**，**不**约束中间的工具调用。
- 下面会给你图片的本地路径，你**必须**先用 Read 工具把每一张逐张读完再动笔，
  一张都不能跳过；跳过直接交空 records 是错误行为。
- 手机拍的表格经常是**横放或倒放**的，先看清方向再逐行读，别把行读串。
- 读完之后，最终回答只留那一个 JSON 对象。"""


# ─────────────────────────── 槽位复校（第②道闸） ───────────────────────────
#
# 🔴 LLM 给的值一律不可信，这里全部过一遍；校验不过就当没有（None）。


def _num(v):
    """→ float 或 None。字符串带「节/元/+」也吃得下；解析不出一律 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("＋", "+").replace("－", "-")
    s = re.sub(r"[^\d.+\-]", "", s)
    if s in ("", "+", "-", ".", "+.", "-."):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _date(v):
    """→ 'YYYY-MM-DD' 或 None。必须是真日历日（2026-02-31 直接判废）。"""
    if not v:
        return None
    s = str(v).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    try:
        datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None
    return s


def _time(v):
    """→ 'HH:mm' 或 None（容忍 '13:30:00' / '13：30'）。"""
    if not v:
        return None
    s = str(v).strip().replace("：", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return "%02d:%02d" % (hh, mm)


def _sid(v):
    """场次 id → 纯数字串或 None。"""
    if v is None:
        return None
    s = re.sub(r"\D", "", str(v))
    return s if len(s) >= 2 else None


def _text(v, limit=200):
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] or None


def _time_note(v):
    """'13:30-15:00' → 归一化；读不出 None。"""
    if not v:
        return None
    s = str(v).strip().replace("：", ":")
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–~～至到]\s*(\d{1,2}:\d{2})", s)
    if not m:
        return None
    a, b = _time(m.group(1)), _time(m.group(2))
    return "%s-%s" % (a, b) if a and b else None


def _resolve_students(names, roster):
    """🔴 第①道闸：LLM 只准给名字，id 一律由代码回花名册解析。

    复用 intents.match_students（全名精确 → 2 字片段模糊），查不到就返空，
    由调用方走原有的「从花名册挑一个」追问链路。
    """
    out, seen = [], set()
    for n in (names or []):
        s = str(n or "").strip()
        if not s or s.isdigit():        # 纯数字 = 它在偷偷给 id，丢掉
            continue
        for r in I.match_students(s, roster):
            if r.get("id") not in seen:
                seen.add(r.get("id"))
                out.append(r)
    return out


def _clean_slots(raw, roster, today):
    """LLM 的 slots → 干净 slots（复校 + id 解析 + 兜底再解析）。"""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "students": _resolve_students(raw.get("students"), roster),
        "subject": I.parse_subject(str(raw.get("subject") or "")),
        "date": _date(raw.get("date")),
        "dateFrom": _date(raw.get("dateFrom")),
        "dateTo": _date(raw.get("dateTo")),
        # 🔴 PRD-018 三档 + 每节时长 + 业务日期：LLM 说了就得接得住。
        #    漏接一个键 = 老师说的那句话在这里被静默丢掉（occurDate 尤其：
        #    正则兜底根本不解析它，这里不接就永远传不到 executor）。
        "hours": _num(raw.get("hours")),
        "lessons": _num(raw.get("lessons")),
        "hoursPerLesson": _num(raw.get("hoursPerLesson")),
        "occurDate": _date(raw.get("occurDate")),
        "amount": _num(raw.get("amount")),
        "price": _num(raw.get("price")),
        "sessionId": _sid(raw.get("sessionId")),
        "timeNote": _time_note(raw.get("timeNote")),
        "note": _text(raw.get("note"), 120),
        "brief": _text(raw.get("brief"), 1000),
        # 🔴 只认真正的 True（字符串 "false"、0、None 一律 False），别让一个含糊值把充值吞掉
        "noRecharge": raw.get("noRecharge") is True or str(raw.get("noRecharge")).lower() == "true",
    }


def _backfill(slots, text, roster, today):
    """🔴 兜底再解析：LLM 漏掉的槽位，用 intents 的正则解析件再捞一次。

    口径 = **LLM 优先，正则补位**。intents 那票 parse_* 函数现在只干这个 +
    格式复校，它们从来没有资格决定 intent（那正是翻车的老路）。
    """
    fb = I.extract(text or "", roster, today)
    for k, v in fb.items():
        if k == "raw":
            continue
        if slots.get(k) in (None, [], ""):
            slots[k] = v
    slots.setdefault("students", [])
    slots["raw"] = text or ""
    return slots


# ─────────────────────────── 花名册摘要 ───────────────────────────


def roster_brief(roster, limit=40):
    """→ 「名字 + 学科 + 余额摘要」紧凑列表（🔴 不喂 id，模型没机会输出 id）。"""
    lines = []
    for st in (roster or [])[:limit]:
        name = (st.get("name") or "").strip()
        if not name:
            continue
        bits = []
        for a in (st.get("accounts") or []):
            # 🔴 PRD-018：hoursRemain 是**小时**，别再当「节」念（模型看这行学口径）
            bits.append("%s余%s" % (a.get("subjectLabel") or "?", X.bal_text(a)))
        grade = st.get("grade") or ""
        lines.append("- %s%s%s" % (name, ("（%s）" % grade) if grade else "",
                                   ("｜" + "，".join(bits)) if bits else "｜未开户"))
    return "\n".join(lines) or "（花名册为空）"


# ─────────────────────────── 主入口 ───────────────────────────


def understand(text, roster, today, img_paths=None, has_pending=False):
    """一句话 → {'intent', 'slots', 'clarify', 'degraded'}。

    intent 必在 INTENTS 白名单内；degraded=True 表示理解模块没跑通
    （此时 intent 固定 unknown，调用方必须回 DEGRADED_REPLY，**不许猜、不许执行**）。

    🔴 路由 pass 不读图——只告诉模型「手上有 N 张待处理的图」这个事实，
    不给路径也不让它 Read。读图是第二段（read_lesson_log）的活，省时间。
    """
    t = (text or "").strip()
    n_img = len(img_paths or [])
    prompt = (
        u"【今天】%s（%s）\n"
        u"【老师手上待处理的图片】%d 张%s\n"
        u"【有没有一条正在等他确认的预览】%s\n"
        u"【花名册（只有这些学生，名字要对得上）】\n%s\n\n"
        u"【老师刚说的这句话】\n%s\n\n"
        u"按系统提示词的口径输出那个 JSON。"
        % (today.isoformat(), u"一二三四五六日"[today.weekday()],
           n_img, u"（他大概率想让你拿这些图做点什么）" if n_img else "",
           u"有" if has_pending else u"没有",
           roster_brief(roster), t)
    )
    raw = X.run_claude(prompt, system=ROUTER_SYSTEM, timeout=BRAIN_TIMEOUT)
    obj = X.extract_json(raw)
    if not isinstance(obj, dict) or not obj.get("intent"):
        # 🔴 降级：不猜、不执行、不回落旧正则
        X.log("[brain] 理解层没产出可用 JSON（raw=%r）→ 降级" % (raw or "")[:200])
        return {"intent": I.UNKNOWN, "slots": _backfill({}, t, roster, today),
                "clarify": None, "degraded": True}

    intent = str(obj.get("intent") or "").strip()
    if intent not in INTENTS:                      # 🔴 第③道闸：白名单
        X.log("[brain] 意图不在白名单：%r → unknown" % intent[:40])
        intent = I.UNKNOWN
    slots = _backfill(_clean_slots(obj.get("slots"), roster, today), t, roster, today)
    clarify = _text(obj.get("clarify"), 300) if intent == I.UNKNOWN else None
    X.log("[brain] intent=%s students=%s slots=%s clarify=%r"
          % (intent, [s.get("name") for s in slots["students"]],
             {k: v for k, v in slots.items()
              if v not in (None, [], "") and k not in ("raw", "students")},
             (clarify or "")[:60]))
    return {"intent": intent, "slots": slots, "clarify": clarify, "degraded": False}


# ─────────────────────────── ⑧ 读图 pass ───────────────────────────


def read_lesson_log(img_paths, today, student_name=None):
    """手写课时本照片 → {'records':[...], 'recharges':[...], 'notes':str, 'ok':bool}。

    records 每条 = {date, start, end, title, lessons, hours}；recharges 每条 =
    {date, lessons, hours, amount, note}。
    🔴 PRD-018 单位纪律：`lessons`=节（本子上的口径），`hours`=小时（底账口径），
    两者都原样带出，**折算交调用方按该学生每节时长做**，这里一个字都不算。
    🔴 看不清的字段是 None，调用方必须把「读不全的行」显式摊给老师看，不许自己补齐。
    """
    imgs = list(img_paths or [])
    if not imgs:
        return {"records": [], "recharges": [], "notes": "", "ok": False}
    prompt = (
        u"【今天】%s\n"
        u"【学生】%s\n"
        u"【图片本地路径，共 %d 张，必须逐张 Read 完】\n%s\n\n"
        u"把这本课时记录逐行读成系统提示词里那个 JSON。看不清的字段填 null，不要猜。"
        % (today.isoformat(), student_name or u"（未指明）", len(imgs), "\n".join(imgs))
    )
    raw = X.run_claude(prompt, system=LESSON_LOG_SYSTEM, timeout=READ_TIMEOUT)
    obj = X.extract_json(raw)
    if not isinstance(obj, dict):
        X.log("[brain] 课时本读图没产出可用 JSON（raw=%r）" % (raw or "")[:200])
        return {"records": [], "recharges": [], "notes": "", "ok": False}

    records = []
    for r in (obj.get("records") or []):
        if not isinstance(r, dict):
            continue
        records.append({
            "date": _date(r.get("date")),
            "start": _time(r.get("start")),
            "end": _time(r.get("end")),
            "title": _text(r.get("title"), 80),
            "lessons": _num(r.get("lessons")),
            "hours": _num(r.get("hours")),
        })
    recharges = []
    for r in (obj.get("recharges") or []):
        if not isinstance(r, dict):
            continue
        h = _num(r.get("hours"))
        ls = _num(r.get("lessons"))
        a = _num(r.get("amount"))
        if h is None and ls is None and a is None:
            continue
        recharges.append({"date": _date(r.get("date")), "hours": h, "lessons": ls,
                          "amount": a, "note": _text(r.get("note"), 80)})
    X.log("[brain] 课时本读出 records=%d recharges=%d" % (len(records), len(recharges)))
    return {"records": records, "recharges": recharges,
            "notes": _text(obj.get("notes"), 300) or "", "ok": True}
