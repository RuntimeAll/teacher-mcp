# -*- coding: utf-8 -*-
"""PRD-016 教务速办机器人（飞书新应用「备课帮-教务」cli_aae002 · 指令侧）。

链路：飞书长连接收单聊消息 → 白名单闸 → SOP 路由（intents）→ 预览/确认闸 → 执行（executor）→ 回复。
推送侧 = 同目录 push_settle.py（每晚推待结算），两者共用 /opt/jiaowu-push/.env。

🔴 边界：
- 老机器人 /root/feishu-mcp-bot/（旧应用 cli_aa985…，PRD-007/009）一个字节不改；
  两个 app 各开各的长连接、各收各的事件流，互不抢。
- BE 零改动、H5 零改动。
- 🔴 2026-08-01 换脑：**理解层**（这句话是什么意图）交 LLM = brain.py；
  **执行层**一个字没松——动作集固定、写库前必预览、必须「确认」才落库、
  10 分钟过期、重复确认幂等。LLM 只产结构化意图和文案，永远不直接碰数据。
- 动作集之外一律说「我不会」（不硬猜一个最像的去做），理解层跑不通就诚实降级。
- 🔴 2026-08-01 止血：**事件处理一律异步**。on_message 只解析 + 去重 + 起线程就返回，
  处理链跑在后台 daemon 线程里（详见「事件闸」一节）。同步处理 = 飞书拿不到 ACK
  = 同一条事件被无限重投，每趟真金白银。--sim 走 handle_text 仍是同步（回归要拿输出）。

跑法：
  python3 jiaowu_bot.py            # 常驻（systemd jiaowu-bot.service）
  python3 jiaowu_bot.py --sim "帮我查下待结算"   # 模拟事件注入（回归自测，不连飞书）
"""

import collections
import datetime
import io
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intents as I           # noqa: E402
import executor as X          # noqa: E402
import brain as B             # noqa: E402
from executor import BeError, log, money, h5, rows_text  # noqa: E402

ENV_PATH = os.environ.get("JIAOWU_ENV", "/opt/jiaowu-push/.env")
MCP_ENV_PATH = os.environ.get("TEACHER_MCP_ENV", "/opt/teacher-mcp/.env")
IMG_DIR = os.environ.get("BOT_IMG_DIR", "/opt/jiaowu-push/img")
CONFIRM_TTL = int(os.environ.get("CONFIRM_TTL", "600"))    # 确认态 10 分钟（D2）
IMG_TTL = int(os.environ.get("IMG_TTL", "1800"))           # 图队列僵尸 30 分钟自清
SEEN_MAX = int(os.environ.get("SEEN_MAX", "500"))          # 已处理 message_id 记忆条数上限
SEEN_TTL = int(os.environ.get("SEEN_TTL", "1800"))         # 已处理 message_id 记忆时效（秒）

# 🔴 0 秒回执（老 bot PRD-007 停役前的体验遗产）+ 路由透明化（2026-08-01 换脑后加的）：
#    ① 理解层本身要跑一次 headless CLI（数秒），静默期必须先顶一句，否则又是"发完干等"。
#    ② 理解完先把「我听成了什么」说出来——上一版翻车时老师完全看不见机器人的判断，
#       只看到一个错结果。现在判断错了他第一时间就能喊停，而不是等到结果出来。
#    写反馈两类不在此表（act_feedback / act_ingest_lesson_log 自带开场白，免连发两条）。
_ACK_UNDERSTAND = "收到 ✓ 让我先听懂你要什么…"

_ACK = {
    "settle_pending": "🧠 听成「查待结算」，正在查…",
    "settle_do":      "🧠 听成「执行结算」，正在核对场次与单价…",
    "ledger":         "🧠 听成「查台账」，正在查…",
    "family_ledger":  "🧠 听成「查这一家的合并余额」，正在合两户…",
    "family_recharge": "🧠 听成「家庭充值」，正在算…",
    "family_rebalance": "🧠 听成「家庭归账对倒」，正在算…",
    "account_open":   "🧠 听成「开户/改单价」，正在准备…",
    "account_recharge": "🧠 听成「充值」，正在算…",
    "account_adjust": "🧠 听成「账户调整」，正在准备…",
    "leave":          "🧠 听成「请假冲正」，正在查这节课…",
}

# ───────────────────────────── 全局态 ─────────────────────────────

_env = X.load_env(ENV_PATH)
_mcp = X.load_env(MCP_ENV_PATH)
WHITELIST = {x for x in [(_env.get("FEISHU_USER_OPENID") or "").strip()] if x}

BE = None            # 懒建（--sim 与常驻都用同一个）
_lock = threading.RLock()
_pending = None      # 待确认动作：{'op','args','preview','ts','done','result'}
_choice = None       # 待消歧：{'kind','items','text','pre','ts'}
# 🔴 待消费图片队列（2026-08-01 正名）：不再是"反馈专用队列"。
#    收图时**不做任何思考**（不调 LLM、不猜图型），图先进队列等着，
#    等下一条文本来了再由理解层决定谁来消费它；被某个动作消费掉就清空。
_images = []         # [{'path','ts'}]
_turn = None         # 本轮理解层结果 {'intent','slots'}，供 _ask_choice 原样带走免二次理解

# 🔴 事件去重表（2026-08-01 止血）：message_id → 首见时间戳。
#    _seen_lock 是**独立**的小锁，绝不能用 _lock——_lock 会被后台线程占住几分钟，
#    去重判定要在毫秒内做完（它在 ACK 主路径上）。
_seen = collections.OrderedDict()
_seen_lock = threading.Lock()


def be():
    global BE
    if BE is None:
        need = [k for k in ("TEACHER_BOT_OPENID",) if not _env.get(k)]
        if need or not _mcp.get("BOT_SECRET"):
            raise BeError("缺配置：%s" % (need + ([] if _mcp.get("BOT_SECRET") else ["BOT_SECRET"])))
        BE = X.Be(_env["TEACHER_BOT_OPENID"], _mcp["BOT_SECRET"], _mcp.get("RUOYI_CLIENT_ID"))
    return BE


def today():
    return datetime.date.today()


# ───────────────────────────── 回复通道 ─────────────────────────────


class Replier(object):
    """回复通道抽象——真机=飞书，回归自测=收集器。handle() 只认这个接口。"""

    def text(self, s):
        raise NotImplementedError

    def image(self, data):
        raise NotImplementedError


class CollectReplier(Replier):
    """--sim 用：只收集不外发（模拟事件注入回归）。"""

    def __init__(self):
        self.texts = []
        self.images = []

    def text(self, s):
        self.texts.append(s)
        print("--- BOT ---\n%s\n" % s, flush=True)

    def image(self, data):
        self.images.append(len(data))
        print("--- BOT IMAGE %d bytes ---" % len(data), flush=True)


class LarkReplier(Replier):
    def __init__(self, client, message_id, chat_id):
        self.c, self.mid, self.cid = client, message_id, chat_id

    def text(self, s):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        req = (ReplyMessageRequest.builder().message_id(self.mid)
               .request_body(ReplyMessageRequestBody.builder()
                             .content(json.dumps({"text": s}, ensure_ascii=False))
                             .msg_type("text").build()).build())
        r = self.c.im.v1.message.reply(req)
        if not r.success():
            log("[回复失败] code=%s msg=%s" % (r.code, r.msg))

    def image(self, data):
        # 🔴 lark SDK 要文件对象（multipart）；裸 bytes = 234011 Can't recognize image format
        from lark_oapi.api.im.v1 import (CreateImageRequest, CreateImageRequestBody,
                                         CreateMessageRequest, CreateMessageRequestBody)
        up = self.c.im.v1.image.create(
            CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message")
                .image(io.BytesIO(data)).build()).build())
        if not up.success():
            log("[图上传失败] code=%s msg=%s" % (up.code, up.msg))
            return
        snd = self.c.im.v1.message.create(
            CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
                CreateMessageRequestBody.builder().receive_id(self.cid).msg_type("image")
                .content(json.dumps({"image_key": up.data.image_key})).build()).build())
        if not snd.success():
            log("[发图失败] code=%s msg=%s" % (snd.code, snd.msg))


# ───────────────────────────── 图队列 ─────────────────────────────


def _img_prune():
    global _images
    now = time.time()
    _images = [e for e in _images if now - e["ts"] <= IMG_TTL]


def img_add(path):
    _img_prune()
    _images.append({"path": path, "ts": time.time()})
    return len(_images)


def img_paths():
    _img_prune()
    return [e["path"] for e in _images]


def img_clear():
    del _images[:]


# ───────────────────────────── 确认闸 ─────────────────────────────


def set_pending(op, args, preview):
    """挂起一个写操作 → 覆盖式（新预览一出，旧预览立即失效 = AC3「只对最近一次有效」）。"""
    global _pending, _choice
    _choice = None
    _pending = {"op": op, "args": args, "preview": preview, "ts": time.time(),
                "done": False, "result": None}
    return (preview + "\n\n—— 回「确认」我就执行；回「取消」放弃。"
            "（此预览 %d 分钟内有效）" % (CONFIRM_TTL // 60))


def do_confirm(rep):
    global _pending
    p = _pending
    if not p:
        rep.text("现在没有待确认的操作。要做什么直接说，比如「查下待结算」。")
        return
    if p["done"]:
        # 幂等：重复「确认」不二次执行（AC3）
        rep.text("这条刚刚已经执行过了，没有重复执行。\n\n%s" % p["result"])
        return
    if time.time() - p["ts"] > CONFIRM_TTL:
        _pending = None
        rep.text("这条预览已经超过 %d 分钟过期了，没有执行。请把指令重发一次。" % (CONFIRM_TTL // 60))
        return
    try:
        res = X.run_op(be(), p["op"], p["args"])
    except BeError as e:
        _pending = None
        rep.text("执行失败：%s\n（没有落库，可以修正后重发指令。）" % e)
        return
    except Exception as e:  # noqa: BLE001
        _pending = None
        log("[执行异常] %r" % e)
        rep.text("执行出错：%r" % e)
        return
    p["done"] = True
    p["result"] = res["text"]
    # 🔴 任何 op 都可以消费图队列（不只反馈）：落库成功且确实用了图 → 清队列免二次误用。
    #    没被消费的图保留，等 IMG_TTL 自然过期。
    if p["args"].get("usedImages"):
        img_clear()
    rep.text(res["text"])
    if res.get("image_path"):
        try:
            data = be().download(res["image_path"])   # 🔴 artifact 端点要 token
            rep.image(data)
        except Exception as e:  # noqa: BLE001
            log("[产物下载失败] %r" % e)
            rep.text("（家长版图没取到：%r，可到 H5 反馈页手动导出）" % e)


# ───────────────────────────── 槽位落地 ─────────────────────────────


def _ask_choice(rep, kind, items, labeler, text, question, pre=None):
    """挂一个「回序号」的消歧问句。

    pre = 本轮理解层已经算出的 {'intent','slots'}——回序号后原样复用，
    🔴 不再拿原文重跑一次 LLM（多花一次往返、还可能这次判成别的意图）。
    """
    global _choice, _pending
    _pending = None
    _choice = {"kind": kind, "items": items, "text": text,
               "pre": pre if pre is not None else _turn, "ts": time.time()}
    lines = [question]
    for i, it in enumerate(items, 1):
        lines.append("  %d. %s" % (i, labeler(it)))
    lines.append("（回一个序号就行）")
    rep.text("\n".join(lines))


def resolve_student(rep, slots, text, forced=None):
    """→ 花名册行；歧义时挂 choice 并返 None。"""
    if forced:
        return forced
    hits = slots.get("students") or []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _ask_choice(rep, "student", hits, lambda s: "%s（%s）" % (s.get("name"), s.get("grade") or ""),
                    text, "这条指令里有多个学生匹配，是哪一个？")
        return None
    roster = be().roster()
    _ask_choice(rep, "student", roster, lambda s: "%s（%s）" % (s.get("name"), s.get("grade") or ""),
                text, "没听出是哪个学生，从花名册里挑一个：")
    return None


def pick_subject(slots, student):
    if slots.get("subject"):
        return slots["subject"]
    accs = student.get("accounts") or []
    if len(accs) == 1 and accs[0].get("subjectLabel"):
        return accs[0]["subjectLabel"]
    return student.get("subjectLabel") or "数学"


# ───────────────────────────── 六意图 ─────────────────────────────


def act_ledger(rep, slots, text, forced_student=None):
    """⑤ 查台账 / 流水 / 余额（只读，直接回）。"""
    st = resolve_student(rep, slots, text, forced_student)
    if not st:
        return
    accs = st.get("accounts") or []
    if not accs:
        rep.text("%s 还没有开户。要开的话说一句：给%s开个数学户 350 一节。" % (st["name"], st["name"]))
        return
    out = ["📊 %s 的账户" % st["name"]]
    for a in accs:
        out.append("· %s：余 %s 课时 / %s 元（单价 %s 元/节，%s）"
                   % (a.get("subjectLabel"), money(a.get("hoursRemain")), money(a.get("amountRemain")),
                      money(a.get("lessonPrice")), "启用" if a.get("status") == "0" else "停用"))
    main = be().account_of(st, slots.get("subject")) or accs[0]
    lg = be().ledger(main["id"], 8) or {}
    rows = lg.get("rows") or []
    if rows:
        out.append("")
        out.append("最近 %d 条流水（共 %s 条）：" % (len(rows), lg.get("total")))
        kind = {"1": "充值", "2": "扣课", "3": "冲正", "4": "调整"}
        for r in rows:
            out.append("  %s %s %s%s 课时 / %s%s 元 ｜ %s"
                       % (r.get("date"), kind.get(str(r.get("flowType")), "?"),
                          "+" if (r.get("hoursDelta") or 0) >= 0 else "", money(r.get("hoursDelta")),
                          "+" if (r.get("amountDelta") or 0) >= 0 else "", money(r.get("amountDelta")),
                          (r.get("content") or "")[:24]))
    out.append("👉 %s" % h5("account"))
    rep.text("\n".join(out))


def act_settle_pending(rep):
    """② 查待办（只读，直接回）。"""
    rows = be().pending()
    if not rows:
        rep.text("✅ 没有待结算的场次，清爽。\n👉 %s" % h5("settle"))
        return
    out = ["📋 待结算 %d 场：" % len(rows)]
    total = 0.0
    for r in rows:
        p = r.get("price")
        total += float(p or 0)
        out.append("· %s %s %s %s-%s ｜ %s%s"
                   % (r.get("date"), r.get("targetName"), r.get("subjectLabel"),
                      r.get("start"), r.get("end"),
                      ("%s 元/节" % money(p)) if p is not None else "⚠️未开户",
                      "" if r.get("accountStatus") in (None, "0") else "（账户停用）"))
    out.append("")
    out.append("按每场 1 课时算，合计约 %s 元。" % money(total))
    out.append("要结就说「把待办清了」（可加：按 1.5 课时扣 / 只结俊羽的）。")
    out.append("👉 %s" % h5("settle"))
    rep.text("\n".join(out))


def act_settle_do(rep, slots, text, forced_student=None):
    """② 一键结算 → 预览（写，需确认）。"""
    rows = be().pending()
    if not rows:
        rep.text("没有待结算的场次，不用结。")
        return
    sid = slots.get("sessionId")
    if sid:
        rows = [r for r in rows if str(r.get("sessionId")) == str(sid)]
    name = None
    if forced_student:
        name = forced_student.get("name")
    elif slots.get("students"):
        if len(slots["students"]) > 1:
            _ask_choice(rep, "student", slots["students"], lambda s: s.get("name"), text,
                        "要只结哪个学生的？")
            return
        name = slots["students"][0].get("name")
    if name:
        rows = [r for r in rows if r.get("targetName") == name]
    if not rows:
        rep.text("按这个条件筛下来没有待结算场次。发「待结算」看看全量。")
        return
    hours = slots.get("hours") or 1.0
    note = slots.get("timeNote")
    items, lines, tot_h, tot_a, warn = [], [], 0.0, 0.0, []
    for r in rows:
        it = {"sessionId": str(r["sessionId"]), "hours": hours}
        if note:
            it["timeNote"] = note
        items.append(it)
        p = r.get("price")
        amt = (float(p) * hours) if p is not None else None
        tot_h += hours
        tot_a += amt or 0
        lines.append("· %s %s %s %s-%s → 扣 %s 课时 / %s"
                     % (r.get("date"), r.get("targetName"), r.get("subjectLabel"),
                        r.get("start"), r.get("end"), money(hours),
                        ("%s 元" % money(amt)) if amt is not None else "⚠️未开户，会被跳过"))
        if p is None:
            warn.append(r.get("targetName"))
    pv = ["【待确认 · 一键结算】共 %d 场" % len(items)] + lines
    pv.append("")
    pv.append("合计：扣 %s 课时 / %s 元" % (money(tot_h), money(tot_a)))
    if note:
        pv.append("实际上课时间备注：%s" % note)
    if warn:
        pv.append("⚠️ %s 没开户，这几场会被系统跳过（不扣）。" % "、".join(sorted(set(warn))))
    # expect* = 预览侧算出的期望值。🔴 BE 的 /settle 只回「成功场次数」不回逐场明细，
    # 回执里的「共扣多少」只能拿这里的期望值报（全成功时才等于实际）。
    rep.text(set_pending("settle", {"items": items, "expectHours": tot_h,
                                    "expectAmount": tot_a}, "\n".join(pv)))


def act_account(rep, intent, slots, text, forced_student=None):
    """① 开户 / 充值 / 调整 → 预览（写，需确认）。"""
    st = resolve_student(rep, slots, text, forced_student)
    if not st:
        return
    subject = pick_subject(slots, st)
    acc = be().account_of(st, subject)
    hours, amount, price = slots.get("hours"), slots.get("amount"), slots.get("price")

    if intent == I.ACCOUNT_OPEN:
        if price is None:
            price = float(acc["lessonPrice"]) if acc and acc.get("lessonPrice") is not None else None
        if price is None:
            rep.text("开户要单价。说全一点，比如：给%s开个%s户 350 一节。" % (st["name"], subject))
            return
        if hours and amount is None:
            amount = hours * price
        if amount and not hours and price:
            hours = round(amount / price, 2)
        pv = ["【待确认 · %s】" % ("改单价" if acc else "开户"),
              "学生：%s" % st["name"], "学科：%s" % subject,
              "单价：%s 元/节%s" % (money(price),
                                 ("（原 %s）" % money(acc.get("lessonPrice"))) if acc else "")]
        if hours or amount:
            pv.append("顺手充值：+%s 课时 / +%s 元" % (money(hours or 0), money(amount or 0)))
        if acc:
            pv.append("当前余额：%s 课时 / %s 元" % (money(acc.get("hoursRemain")), money(acc.get("amountRemain"))))
        args = {"studentId": st["id"], "studentName": st["name"], "subject": subject,
                "price": price, "hours": hours, "amount": amount, "note": slots.get("note")}
        rep.text(set_pending("account_open", args, "\n".join(pv)))
        return

    if not acc:
        rep.text("%s 还没有 %s 账户，先开户：给%s开个%s户 350 一节。"
                 % (st["name"], subject, st["name"], subject))
        return
    price = float(acc.get("lessonPrice") or 0)
    if intent == I.ACCOUNT_RECHARGE:
        if hours is None and amount is None:
            rep.text("充多少？说个数，比如：%s充 20 节，或 %s充值 7000 元。" % (st["name"], st["name"]))
            return
        if hours is not None and amount is None:
            amount = hours * price
        if amount is not None and hours is None:
            hours = round(amount / price, 2) if price else 0
        flow_type, kind = "1", "充值"
    else:
        if hours is None and amount is None:
            rep.text("调整多少？带上正负号，比如：%s调整 -1 节 备注 补扣。" % st["name"])
            return
        if hours is not None and amount is None:
            amount = hours * price
        if amount is not None and hours is None:
            hours = 0.0
        flow_type, kind = "4", "调整"
    pv = ["【待确认 · %s】" % kind, "学生：%s ｜ 学科：%s" % (st["name"], subject),
          "课时：%s%s 节" % ("+" if hours >= 0 else "", money(hours)),
          "金额：%s%s 元（单价 %s）" % ("+" if amount >= 0 else "", money(amount), money(price)),
          "当前余额：%s 课时 / %s 元" % (money(acc.get("hoursRemain")), money(acc.get("amountRemain"))),
          "执行后约：%s 课时 / %s 元" % (money(float(acc.get("hoursRemain") or 0) + hours),
                                    money(float(acc.get("amountRemain") or 0) + amount))]
    if slots.get("note"):
        pv.append("备注：%s" % slots["note"])
    args = {"accountId": acc["id"], "studentId": st["id"], "studentName": st["name"],
            "subject": subject, "flowType": flow_type, "hours": hours, "amount": amount,
            "note": slots.get("note")}
    rep.text(set_pending("account_flow", args, "\n".join(pv)))


def act_leave(rep, slots, text, forced_student=None, forced_session=None):
    """③ 请假冲正 → 预览（写，需确认）。"""
    ses = forced_session
    if not ses and slots.get("sessionId"):
        st_rows = []
        for s in be().roster():
            st_rows += be().sessions(s["id"])
        ses = next((s for s in st_rows if str(s.get("id")) == str(slots["sessionId"])), None)
        if not ses:
            rep.text("没找到场次 %s。" % slots["sessionId"])
            return
    if not ses:
        st = resolve_student(rep, slots, text, forced_student)
        if not st:
            return
        cands = [s for s in be().sessions(st["id"]) if s.get("sessionStatus") in ("0", "1")]
        if slots.get("date"):
            cands = [s for s in cands if str(s.get("sessionDate")) == slots["date"]]
        if not cands:
            rep.text("%s 在%s没有可请假的场次（已请假/已取消的不算）。"
                     % (st["name"], "该日期" if slots.get("date") else "系统里"))
            return
        if len(cands) > 1:
            cands = sorted(cands, key=lambda s: (str(s.get("sessionDate")), str(s.get("startTime"))),
                           reverse=True)[:8]
            _ask_choice(rep, "session", cands,
                        lambda s: "%s %s-%s %s" % (s.get("sessionDate"), str(s.get("startTime"))[:5],
                                                   str(s.get("endTime"))[:5], s.get("lessonTitle") or ""),
                        text, "%s 有多场，请假的是哪一场？" % st["name"])
            return
        ses = cands[0]
    desc = "%s %s %s-%s" % (ses.get("targetName"), ses.get("sessionDate"),
                            str(ses.get("startTime"))[:5], str(ses.get("endTime"))[:5])
    settled = str(ses.get("settleStatus")) == "1"
    pv = ["【待确认 · 请假冲正】", "场次：%s（#%s）" % (desc, ses.get("id")),
          "课题：%s" % (ses.get("lessonTitle") or "—"),
          "结算状态：%s" % ("已结算 → 请假后自动冲正退回课时和课费" if settled else "未结算 → 只置请假，不动账")]
    rep.text(set_pending("leave", {"sessionId": str(ses["id"]), "desc": desc}, "\n".join(pv)))


# ───────────────────────── ⑦ 家庭钱包（好好 + 俊羽） ─────────────────────────
#
# 🔴 零 BE 改动的「家庭钱包」玩法（2026-07-31 用户拍板，写死这一家不做通用配置）：
#   钱包户 = 俊羽户，家里的钱全额充这里；好好户不充值，一路走负数（系统允许欠费）；
#   家庭真实余额 = 两户 amountRemain 相加。机器人把这套体验包掉。
#   两人单价/时长不同（俊羽 1.5h、好好 1h），所以**共享的是钱不是节**：
#   合并只加金额、归账只倒金额，课时列永远各归各户。

FAMILY = {
    "label": "好好 + 俊羽（一家）",
    "wallet_id": "60",                 # 俊羽 = 家庭钱包户
    "wallet_name": "俊羽",
    "member_ids": ["60", "59"],        # 俊羽、好好（合计口径按此顺序展示）
}


def _family_members():
    """→ [(学生行, 账户 or None)]，按 FAMILY.member_ids 顺序；缺员静默跳过。"""
    by_id = {str(s.get("id")): s for s in be().roster()}
    out = []
    for mid in FAMILY["member_ids"]:
        st = by_id.get(mid)
        if st:
            out.append((st, be().account_of(st, None)))
    return out


def _family_total(members):
    return sum(float((acc or {}).get("amountRemain") or 0) for _, acc in members)


def act_family_ledger(rep):
    """⑦-1 合并查询：合计一行 + 各自明细。只读免确认。"""
    members = _family_members()
    if not members:
        rep.text("花名册里没找到这一家的学生，先确认档案还在。")
        return
    total = _family_total(members)
    out = ["👪 %s · 家庭钱包" % FAMILY["label"],
           "💰 合计余额：%s 元" % money(total), "", "各户明细："]
    for st, acc in members:
        tag = "（家庭钱包户）" if str(st.get("id")) == FAMILY["wallet_id"] else ""
        if not acc:
            out.append("· %s%s：未开户（按 0 计）" % (st.get("name"), tag))
            continue
        amt = float(acc.get("amountRemain") or 0)
        out.append("· %s%s：%s 元 ｜ %s 课时 ｜ 单价 %s 元/节%s"
                   % (st.get("name"), tag, money(amt), money(acc.get("hoursRemain")),
                      money(acc.get("lessonPrice")), "  ⚠️欠费" if amt < 0 else ""))
    out.append("")
    out.append("（钱全额充在%s户，%s户不充、一路走负数；合计=两户相加才是家里的真实余额）"
               % (FAMILY["wallet_name"],
                  "、".join(s.get("name") for s, _ in members
                            if str(s.get("id")) != FAMILY["wallet_id"])))
    debt = [s.get("name") for s, a in members if float((a or {}).get("amountRemain") or 0) < 0]
    if debt:
        out.append("要把 %s 户的负数抹平就说一句「归账」。" % "、".join(debt))
    out.append("👉 %s" % h5("account"))
    rep.text("\n".join(out))


def act_family_recharge(rep, slots):
    """⑦-2 家庭充值 → 全额进钱包户（俊羽），走现有充值链。"""
    members = _family_members()
    wallet = next(((s, a) for s, a in members if str(s.get("id")) == FAMILY["wallet_id"]), None)
    if not wallet:
        rep.text("没找到家庭钱包户（%s），先确认档案。" % FAMILY["wallet_name"])
        return
    st, acc = wallet
    if not acc:
        rep.text("家庭钱包户（%s）还没开户，先说：给%s开个数学户 350 一节。"
                 % (st["name"], st["name"]))
        return
    price = float(acc.get("lessonPrice") or 0)
    hours, amount = slots.get("hours"), slots.get("amount")
    if hours is None and amount is None:
        rep.text("家里充多少？说个数，比如：他们家充 7000。")
        return
    if hours is not None and amount is None:
        amount = hours * price
    if amount is not None and hours is None:
        hours = round(amount / price, 2) if price else 0
    total = _family_total(members)
    pv = ["【待确认 · 家庭充值】",
          "🔴 家庭钱包 = %s户，这笔钱全额充进%s户（另一个孩子的户不充、照常走负数）"
          % (st["name"], st["name"]),
          "学科：%s ｜ 单价：%s 元/节" % (acc.get("subjectLabel") or "数学", money(price)),
          "金额：+%s 元" % money(amount),
          "课时：+%s 节（按%s单价折算，记在%s户）" % (money(hours), st["name"], st["name"]),
          "",
          "%s户：%s 课时 / %s 元 → 约 %s 课时 / %s 元"
          % (st["name"], money(acc.get("hoursRemain")), money(acc.get("amountRemain")),
             money(float(acc.get("hoursRemain") or 0) + hours),
             money(float(acc.get("amountRemain") or 0) + amount)),
          "家庭合计：%s 元 → %s 元" % (money(total), money(total + amount))]
    if slots.get("note"):
        pv.append("备注：%s" % slots["note"])
    args = {"accountId": acc["id"], "studentId": st["id"], "studentName": st["name"],
            "subject": acc.get("subjectLabel"), "flowType": "1", "hours": hours,
            "amount": amount, "note": slots.get("note") or "家庭充值"}
    rep.text(set_pending("account_flow", args, "\n".join(pv)))


def act_family_rebalance(rep):
    """⑦-3 归账对倒：欠费户补齐、钱包户等额扣，一次确认打包两条调整。"""
    members = _family_members()
    wallet = next(((s, a) for s, a in members if str(s.get("id")) == FAMILY["wallet_id"]), None)
    debts = [(s, a) for s, a in members
             if str(s.get("id")) != FAMILY["wallet_id"] and a
             and float(a.get("amountRemain") or 0) < 0]
    if not wallet or not wallet[1]:
        rep.text("家庭钱包户（%s）还没开户，没法归账。" % FAMILY["wallet_name"])
        return
    if not debts:
        others = [s.get("name") for s, a in members if str(s.get("id")) != FAMILY["wallet_id"]]
        rep.text("✅ 无需归账：%s 户余额没有负数（未开户或已 ≥ 0）。\n👉 %s"
                 % ("、".join(others) or "另一个", h5("account")))
        return
    if len(debts) > 1:                   # 本家只有两口，留个防呆
        rep.text("这一家有多个欠费户，机器人只处理单个，请上 H5 手工归账：%s" % h5("account"))
        return
    dst, dacc = debts[0]
    wst, wacc = wallet
    deficit = -float(dacc.get("amountRemain") or 0)
    total = _family_total(members)
    pv = ["【待确认 · 家庭归账（两条调整打包）】",
          "%s户欠 %s 元 → 从「%s户（家庭钱包）」等额对倒平掉。" % (dst["name"], money(deficit), wst["name"]),
          "",
          "① %s户：金额 +%s 元 → %s 元（归零）" % (dst["name"], money(deficit),
                                            money(float(dacc.get("amountRemain") or 0) + deficit)),
          "② %s户：金额 -%s 元 → %s 元" % (wst["name"], money(deficit),
                                       money(float(wacc.get("amountRemain") or 0) - deficit)),
          "备注都记「家庭归账」。",
          "",
          "🔴 只倒金额列，两边课时列都不动——两人单价时长不同，课时跨户搬就失真了；",
          "   家里共享的是钱不是节。%s户课时仍是 %s（她自己那本账照旧）。"
          % (dst["name"], money(dacc.get("hoursRemain"))),
          "家庭合计余额：%s 元 → %s 元（对倒不改变合计，只是换个口袋）"
          % (money(total), money(total))]
    args = {"debtAccountId": dacc["id"], "debtName": dst["name"],
            "walletAccountId": wacc["id"], "walletName": wst["name"],
            "amount": deficit, "memberIds": FAMILY["member_ids"], "note": "家庭归账"}
    rep.text(set_pending("family_rebalance", args, "\n".join(pv)))


def _find_sheet(be_, target_id, lesson_date, session_id=None):
    """同日/同场已有反馈单（含结算生成的空壳）→ 复用其 id，避免建重复单。"""
    for s in be_.sheets(target_id):
        if session_id and str(s.get("sessionId")) == str(session_id):
            return s
        if lesson_date and str(s.get("lessonDate")) == str(lesson_date):
            return s
    return None


def act_feedback(rep, slots, text, use_images, forced_student=None):
    """④⑥ 写反馈 → 生成五列 → 预览（写，需确认）。"""
    imgs = img_paths() if use_images else []
    if use_images and not imgs:
        # 🔴 不再要求他背「生成反馈」这个暗号——理解层听得懂人话了
        rep.text("手上还没有图。先把学生作业/板书照片发给我（可多张），"
                 "再说一句「看图帮我写反馈」。")
        return
    st = resolve_student(rep, slots, text, forced_student)
    if not st:
        return
    date = slots.get("date") or today().isoformat()
    rep.text("收到 ✓ 正在%s整理 %s %s 的反馈单，稍等…"
             % (("逐张看 %d 张图并" % len(imgs)) if imgs else "", st["name"], date))
    rows, source = X.gen_feedback_rows(text, st["name"], date, imgs)
    plan = be().plan_of(st, pick_subject(slots, st))
    plan_id = plan.get("planId") if plan else None
    ses = next((s for s in be().sessions(st["id"]) if str(s.get("sessionDate")) == date), None)
    exist = _find_sheet(be(), st["id"], date, ses.get("id") if ses else None)
    pv = ["【待确认 · %s反馈单】" % ("按图生成的" if imgs else ""),
          "学生：%s ｜ 上课日期：%s" % (st["name"], date)]
    if exist:
        pv.append("动作：更新已有反馈单 #%s（不新建重复单）" % exist.get("id"))
    else:
        pv.append("动作：新建反馈单%s" % ("（挂计划 %s）" % plan.get("planName") if plan else ""))
    pv.append("内容 %d 行：" % len(rows))
    pv.append(rows_text(rows))
    if source == "fallback":
        pv.append("⚠️ 文案生成降级为模板（模型没跑通），内容较粗，确认前请先看一眼。")
    pv.append("确认后我会保存并把家长版图发给你。")
    args = {"targetId": st["id"], "targetName": st["name"], "lessonDate": date, "rows": rows,
            "planId": plan_id, "sessionId": (ses or {}).get("id"),
            "sheetId": (exist or {}).get("id"), "batchKey": (exist or {}).get("batchKey"),
            "title": (exist or {}).get("title"), "usedImages": bool(imgs)}
    rep.text(set_pending("feedback", args, "\n".join(pv)))


# ───────────────────── ⑧ 看图补录课时本（2026-08-01 新增） ─────────────────────
#
# 场景：老师手上有一本手写课时本（纸），拍照发过来，把历史上课记录补进系统。
# 两段式：① 读图 pass（brain.read_lesson_log，逐张 Read）② 预览 → 确认 → 执行。
#
# 🔴 为什么必须逐条摊开让他过目：这是**手写体**。日期/时间读错 = 系统里排错课、扣错钱，
#    而且补录出来的是历史场次，错了要一条条去 H5 删。机器读完只是"线索"，他点头才是事实。


def _sess_key(date, start):
    return "%s %s" % (date, str(start or "")[:5])


def act_ingest_lesson_log(rep, slots, text, forced_student=None):
    """⑧ 看手写课时本 → 补录历史场次 + 扣课时 + 充值笔 → 预览（写，需确认）。"""
    imgs = img_paths()
    if not imgs:
        rep.text("这个得看照片才能办。先把课时本拍给我（可多张），再说一句"
                 "「这是俊羽的课时记录，帮我补录进系统」。")
        return
    st = resolve_student(rep, slots, text, forced_student)
    if not st:
        return
    subject = pick_subject(slots, st)
    acc = be().account_of(st, subject)
    if not acc:
        rep.text("%s 还没有 %s 账户，补录进去也没地方扣课时。先开户：给%s开个%s户 350 一节。"
                 % (st["name"], subject, st["name"], subject))
        return
    price = float(acc.get("lessonPrice") or 0)

    # 🔴 这句必须在读图 pass **开始之前**发：读图动辄 1~3 分钟，中间一个字都没有，
    #    老师会以为又挂了（然后重发一条 = 又一趟钱）。把耗时说死，他才肯等。
    rep.text("收到 ✓ 正在逐行读这 %d 张课时本，手写体要看仔细，大概 1~3 分钟。"
             "读完我把逐条清单发你核对，你点头我才落库——这段时间不用重发，我在读。"
             % len(imgs))
    r = B.read_lesson_log(imgs, today(), st["name"])
    if not r.get("ok"):
        rep.text("这几张图我没读出来（读图模块没跑通），没有落任何库。"
                 "要么重拍清楚一点再发，要么上 H5 手工补：%s" % h5("schedule"))
        return
    recs, notes = r["records"], r.get("notes") or ""
    if not recs:
        rep.text("图我逐张看了，但没认出一条完整的上课记录。%s\n"
                 "换个角度重拍（表格拍正、别反光）再发一次试试。"
                 % (("我读到的困难：%s" % notes) if notes else ""))
        return

    # ── ①a 时间列补位：日期读清了、只有时间没读清 → 拿这张表**自己**最常见的时段补上 ──
    # 🔴 这不是"猜"：日期不可推断（读不出就必须挡下），但一本课时本的上课时段高度一致，
    #    用同一张表里已经读清的多数时段填，是**代码侧有据可查的默认值**，且预览里逐条标出来。
    #    实测踩点：模型在时间列个别字反光时会整行拒读，只因分钟数不确定就丢掉一整节课的账，
    #    而那几行的日期和「扣 1 节」其实都由剩余列 9→0 交叉验过，挡掉太亏。
    def _modal(key):
        vals = [r.get(key) for r in recs if r.get(key)]
        return max(set(vals), key=vals.count) if vals else None

    m_start, m_end = _modal("start"), _modal("end")
    timed = []
    for rc in recs:
        if rc.get("date") and m_start and m_end and not (rc.get("start") and rc.get("end")):
            rc = dict(rc, start=rc.get("start") or m_start, end=rc.get("end") or m_end,
                      _timeGuessed=True)
        timed.append(rc)
    recs = timed

    # ── ① 分拣：读不全的 / 不在他说的日期范围内的 / 系统里已有的 / 真要建的 ──
    d_from, d_to = slots.get("dateFrom"), slots.get("dateTo")
    exist = set()
    for s in be().sessions(st["id"]):
        exist.add(_sess_key(s.get("sessionDate"), s.get("startTime")))

    todo, broken, out_range, dup = [], [], [], []
    for i, rc in enumerate(recs, 1):
        if not (rc.get("date") and rc.get("start") and rc.get("end")):
            broken.append((i, rc))
            continue
        if (d_from and rc["date"] < d_from) or (d_to and rc["date"] > d_to):
            out_range.append(rc)
            continue
        if _sess_key(rc["date"], rc["start"]) in exist:
            dup.append(rc)
            continue
        todo.append(rc)

    if not todo:
        why = []
        if dup:
            why.append("%d 条系统里已经有了" % len(dup))
        if out_range:
            why.append("%d 条不在你说的日期范围内" % len(out_range))
        if broken:
            why.append("%d 条关键字段没读清" % len(broken))
        rep.text("没有需要补录的记录（%s）。没有落任何库。" % ("；".join(why) or "读出来是空的"))
        return

    # ── ② 组装 items / 结算量 / 充值笔 ──
    items, hours_by_key, tot_h, guessed = [], {}, 0.0, []
    for rc in todo:
        hrs = rc.get("hours")
        if hrs is None or hrs <= 0:
            hrs = 1.0
            guessed.append(rc["date"])
        title = rc.get("title") or ""
        items.append({"date": rc["date"], "start": rc["start"], "end": rc["end"],
                      "sessionType": "1", "subject": subject,
                      # 🔴 externalTitle 只有 sessionType='3' 才会被展示，但 '3' 不能结算
                      #    （settleOne 直接拒），所以正课的上课内容只能落 note。两个都写：
                      #    note 是 H5 排课页能看到的那一栏，externalTitle 留作原文存档。
                      "externalTitle": title or None,
                      "note": ("课时本补录：%s" % title) if title else "课时本补录"})
        hours_by_key[_sess_key(rc["date"], rc["start"])] = hrs
        tot_h += hrs
    tot_a = tot_h * price

    # 他明说「只补上课记录、充值我自己充过了」→ 充值笔整块不做（查不了重，只能听他的）
    recharges = []
    for rc in ([] if slots.get("noRecharge") else (r.get("recharges") or [])):
        h, a = rc.get("hours"), rc.get("amount")
        if h is not None and a is None:
            a = h * price
        if a is not None and h is None:
            h = round(a / price, 2) if price else 0
        d = rc.get("date")
        recharges.append({"hours": h, "amount": a,
                          "note": "%s 充值 %s 节（课时本补录）"
                                  % (d or "（日期未读出）", money(h))})

    # ── ③ 预览：逐条摊开让他核 ──
    pv = ["【待确认 · 看课时本补录】学生：%s ｜ 学科：%s ｜ 单价 %s 元/节"
          % (st["name"], subject, money(price)),
          "",
          "要补录这 %d 节课（建场次 + 当场扣课时）：" % len(items)]
    for rc in todo:
        hrs = hours_by_key[_sess_key(rc["date"], rc["start"])]
        flags = []
        if rc.get("_timeGuessed"):
            flags.append("时间没读清，按本子上其它行的 %s-%s 计" % (m_start, m_end))
        if rc["date"] in guessed:
            flags.append("课时数没读出，按 1 节算")
        pv.append("  · %s %s-%s ｜ %s ｜ 扣 %s 节%s"
                  % (rc["date"], rc["start"], rc["end"], rc.get("title") or "（内容没读出）",
                     money(hrs), ("（%s）" % "；".join(flags)) if flags else ""))
    pv.append("")
    pv.append("合计扣：%s 课时 / %s 元" % (money(tot_h), money(tot_a)))
    # 上课内容整列没读出来（手写连笔+照片横放时常见）：账目照样是准的，但内容栏会是空的，
    # 先把「值不值得重拍」这个选择摆给他，别让他确认完才发现内容全空。
    n_blank = sum(1 for rc in todo if not rc.get("title"))
    if n_blank >= max(2, len(todo) // 2):
        pv.append("⚠️ 这 %d 节的**上课内容**我没认出来（手写连笔/照片横放），日期和课时是准的。"
                  "内容栏留空不影响扣课时对账；想要内容就重拍一张摆正放大的、或直接把这几行"
                  "内容打给我，我重新出预览。" % n_blank)
    if dup:
        pv.append("⏭ 跳过 %d 条（系统里同日同时段已经有了，不重复补）：%s"
                  % (len(dup), "、".join("%s %s" % (x["date"], x["start"]) for x in dup[:8])))
    if out_range:
        pv.append("⏭ 跳过 %d 条（不在你说的 %s~%s 范围内）：%s"
                  % (len(out_range), d_from or "…", d_to or "…",
                     "、".join(str(x.get("date")) for x in out_range[:8])))
    if broken:
        pv.append("⚠️ 有 %d 条关键字段没读清，我**没有**猜，也不会补录，请自己看一眼："
                  % len(broken))
        for i, rc in broken[:6]:
            pv.append("     第%d行：日期=%s 时间=%s~%s 内容=%s"
                      % (i, rc.get("date") or "?", rc.get("start") or "?",
                         rc.get("end") or "?", rc.get("title") or "?"))
    if recharges:
        pv.append("")
        pv.append("还要补 %d 笔充值：" % len(recharges))
        for rc in recharges:
            pv.append("  · +%s 课时 / +%s 元 ｜ 备注「%s」"
                      % (money(rc["hours"]), money(rc["amount"]), rc["note"]))
        pv.append("🔴 充值这几行在台账上的**日期会记成今天**——系统的充值流水没有业务日期列，"
                  "真实日期只能写在备注里。上课扣课时那些行不受影响，走的是场次日期，是对的。")
        # 🔴 上课记录能按「同日同时段」查重，充值笔查不了（流水表没有业务日期列，
        #    对不上"这笔是不是就是那笔"）。宁可把这个洞明说给他，也不要闷头充重。
        pv.append("🔴 充值笔我**没法查重**（原因同上：流水表没有业务日期）。"
                  "这几笔里只要有一笔你以前已经充过，就会重复充。已经充过的话回「取消」，"
                  "改说一句「只补上课记录，不补充值」。")
    elif slots.get("noRecharge") and (r.get("recharges") or []):
        pv.append("")
        pv.append("⏭ 图上那 %d 笔充值按你说的**不补**，只补上课记录。"
                  % len(r.get("recharges") or []))
    pv.append("")
    pv.append("🔴 上面是**机器读手写体**的结果，请逐条对着本子核一遍再确认。")
    pv.append("🔴 上课内容会记进场次备注（H5 排课页能看到）；课时流水单的「内容」列"
              "系统固定显示「正课」，那是后端口径，不是没录进去。")
    pv.append("确认后我会：建场次 → 立刻逐场结算 → 补充值笔 → 出一张课时流水单发你核对。"
              "（补录的历史场次会瞬间进「待结算」，所以这三步我不停顿、一口气做完。）")
    if notes:
        pv.append("（我读图时拿不准的地方：%s）" % notes)

    args = {"studentId": st["id"], "studentName": st["name"], "subject": subject,
            "accountId": acc["id"], "items": items, "hoursByKey": hours_by_key,
            "expectAmount": tot_a, "recharges": recharges, "usedImages": True}
    rep.text(set_pending("ingest_lesson_log", args, "\n".join(pv)))


# ───────────────────────────── 路由 ─────────────────────────────


def dispatch(text, rep, forced_student=None, forced_session=None, pre=None):
    """一条文本 → 动作。pre = 已算好的 {'intent','slots'}（回序号复用，免二次理解）。"""
    global _turn
    # ── 第一段：直判（确认/取消/序号）。零歧义、要秒回，不走 LLM ──
    direct = I.detect_direct(text)
    if direct == I.CONFIRM:
        do_confirm(rep)
        return direct
    if direct == I.CANCEL:
        global _pending, _choice
        had = bool(_pending or _choice)
        _pending, _choice = None, None
        rep.text("好，已取消。" if had else "没有正在等确认的操作。")
        return direct
    if direct == I.CHOICE:
        resolve_choice(text, rep)
        return direct

    # ── 第二段：理解层（LLM）。🔴 不认识就说不会，绝不滑到最近的关键词上 ──
    if pre is not None:
        intent, slots, clarify, degraded = pre["intent"], pre["slots"], None, False
        roster = be().roster()
    else:
        rep.text(_ACK_UNDERSTAND)
        roster = be().roster()
        u = B.understand(text, roster, today(), img_paths(), bool(_pending))
        intent, slots, clarify, degraded = (u["intent"], u["slots"],
                                            u.get("clarify"), u.get("degraded"))
    _turn = {"intent": intent, "slots": slots}

    if degraded:
        # 🔴 降级：不猜、不执行、不回落旧关键词表——旧表正是 07-31 翻车的原因
        rep.text(B.DEGRADED_REPLY)
        return "degraded"
    if intent == I.HELP:
        rep.text(I.CAPABILITY_LIST)
        return intent
    if intent == I.UNKNOWN:
        rep.text(((clarify + "\n\n") if clarify else "这个我不会——我不太确定你要做什么。\n\n")
                 + I.CAPABILITY_LIST)
        return intent

    # ── 第三段：执行（动作集固定；写操作一律先预览再等「确认」） ──
    if intent not in (I.FEEDBACK_TEXT, I.FEEDBACK_IMAGE, I.INGEST_LESSON_LOG):
        rep.text(_ACK.get(intent, "收到 ✓ 处理中…"))
    if intent == I.SETTLE_PENDING:
        act_settle_pending(rep)
    elif intent == I.SETTLE_DO:
        act_settle_do(rep, slots, text, forced_student)
    elif intent == I.LEDGER:
        act_ledger(rep, slots, text, forced_student)
    elif intent == I.FAMILY_LEDGER:
        act_family_ledger(rep)
    elif intent == I.FAMILY_RECHARGE:
        act_family_recharge(rep, slots)
    elif intent == I.FAMILY_REBALANCE:
        act_family_rebalance(rep)
    elif intent in (I.ACCOUNT_OPEN, I.ACCOUNT_RECHARGE, I.ACCOUNT_ADJUST):
        act_account(rep, intent, slots, text, forced_student)
    elif intent == I.LEAVE:
        act_leave(rep, slots, text, forced_student, forced_session)
    elif intent == I.INGEST_LESSON_LOG:
        act_ingest_lesson_log(rep, slots, text, forced_student)
    elif intent in (I.FEEDBACK_TEXT, I.FEEDBACK_IMAGE):
        act_feedback(rep, slots, text, intent == I.FEEDBACK_IMAGE, forced_student)
    else:                                   # 白名单已保证到不了这里，留个哑闸
        rep.text(I.CAPABILITY_LIST)
    return intent


def resolve_choice(text, rep):
    global _choice
    c = _choice
    if not c:
        rep.text("现在没有要选的东西。直接说要做什么就行。")
        return
    if time.time() - c["ts"] > CONFIRM_TTL:
        _choice = None
        rep.text("刚才那个选项已经过期了，请把指令重发一次。")
        return
    idx = int(text.strip()) - 1
    if idx < 0 or idx >= len(c["items"]):
        rep.text("序号超出范围，重新回一个 1~%d。" % len(c["items"]))
        return
    picked = c["items"][idx]
    orig, pre = c["text"], c.get("pre")
    _choice = None
    if c["kind"] == "student":
        dispatch(orig, rep, forced_student=picked, pre=pre)
    else:
        dispatch(orig, rep, forced_session=picked, pre=pre)


# ──────────────────── 事件闸：去重 + 异步（2026-08-01 止血） ────────────────────
#
# 🔴 事故复盘（2026-08-01 13:15~13:21，/opt/jiaowu-push/bot.log 实证）：
#    同一条消息被处理了三次，每趟 90~190 秒、每趟真金白银烧 claude CLI。
#    根因 = on_message 是同步的：飞书长连接靠 handler 返回来判投递成功，
#    而 ⑧ 看图补录的读图 pass 要跑 87~188 秒 → 飞书等不到 ACK 判失败 → 重投同一条
#    → 重投又是一趟 188 秒 → 永远追不上，死循环。旧代码的 _lock 还让重投排队串行，
#    越堆越糟。
#
# 两道闸（缺一不可）：
#    ① 异步化（根治）：on_message 只解析 + 去重 + 起线程，毫秒级返回 → ACK 及时。
#    ② 去重（兜底）：飞书是 at-least-once 语义，ACK 再快也可能因网络抖动重投；
#       见过的 message_id 一律丢弃。


def seen_message(message_id):
    """True = 这条 message_id 之前见过（飞书重投）→ 调用方直接丢弃。

    🔴 首见即登记，登记发生在**处理之前**：重投常常在上一趟还没跑完时就到了，
       等处理完再登记等于没去重。
    表有双重上限：SEEN_TTL 秒过期 + 最多 SEEN_MAX 条（OrderedDict 按插入序淘汰队头）。
    """
    if not message_id:
        return False
    now = time.time()
    with _seen_lock:
        for k, ts in list(_seen.items()):      # 插入序 → 队头最旧，遇到没过期的即可停
            if now - ts > SEEN_TTL:
                _seen.pop(k, None)
            else:
                break
        if message_id in _seen:
            return True
        _seen[message_id] = now
        while len(_seen) > SEEN_MAX:
            _seen.popitem(last=False)
        return False


def _bg_run(work, rep, tag):
    """把一条消息的完整处理链丢后台线程。

    🔴 线程里的异常必须被吃掉并落 log + 尽力回一句给老师——后台线程静默死掉
       比报错更糟（他会一直等一个永远不来的回复）。
    """
    def _runner():
        try:
            work()
        except Exception as e:  # noqa: BLE001
            log("[后台线程异常] tag=%s %r" % (tag, e))
            try:
                rep.text("出错了：%r\n（没有落库，可以修正后重发指令。）" % e)
            except Exception as e2:  # noqa: BLE001
                log("[后台异常回复也失败] %r" % e2)
    t = threading.Thread(target=_runner, name="jiaowu-%s" % tag, daemon=True)
    t.start()
    return t


def handle_event(message_id, work, rep, tag="msg"):
    """事件总入口：去重 → 起后台线程 → **立刻返回**（让 SDK 拿到 ACK）。

    这个函数自身必须是毫秒级的：它里面一个字节的网络 IO 都不能有。
    返回 Thread（重投被丢弃时返回 None），仅供回归断言用。
    """
    if seen_message(message_id):
        log("[重投丢弃] message_id=%s" % message_id)
        return None
    return _bg_run(work, rep, tag)


# ───────────────────────────── 消息处理 ─────────────────────────────


def handle_text(open_id, text, rep):
    """白名单 → 路由。所有入口都从这里过（真机 / --sim 共用，保证回归即真行为）。"""
    if open_id not in WHITELIST:
        log("[拒绝] 非白名单 open_id=%s text=%r" % (open_id, (text or "")[:60]))
        rep.text("抱歉，这个机器人只对它的主人开放，我不能替你操作教务数据。")
        return "denied"
    text = (text or "").strip()
    if not text:
        return "empty"
    log("[收到] open_id=%s imgs=%d text=%r" % (open_id, len(img_paths()), text[:120]))
    with _lock:
        try:
            return dispatch(text, rep)
        except BeError as e:
            log("[BE 失败] %s" % e)
            rep.text("系统接口没通：%s\n稍后再试，或上 H5 处理：%s" % (e, h5("schedule")))
            return "error"
        except Exception as e:  # noqa: BLE001
            log("[异常] %r" % e)
            rep.text("出错了：%r" % e)
            return "error"


def handle_image(open_id, data, message_id, rep):
    if open_id not in WHITELIST:
        log("[拒绝·图] 非白名单 open_id=%s" % open_id)
        rep.text("抱歉，这个机器人只对它的主人开放。")
        return "denied"
    ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
    try:
        os.makedirs(IMG_DIR, exist_ok=True)
        # 🔴 文件名前缀 in_（不再是 fb_in_）——fb_ 是老反馈机器人的遗物，
        #    图队列早已不是"反馈专用"，留着前缀会让人以为进来的图都是作业照片。
        path = os.path.join(IMG_DIR, "in_%s%s" % (message_id, ext))
        with open(path, "wb") as fh:
            fh.write(data)
    except Exception as e:  # noqa: BLE001
        log("[存图失败] %r" % e)
        rep.text("图片存盘出错，稍后重发一次。")
        return "error"
    # 🔴 异步化之后收图也要抢 _lock：不然一条正在跑读图 pass 的文本（几分钟）
    #    会被半路插进队列的新图搅浑（act_* 开头才取 img_paths()）。
    with _lock:
        n = img_add(path)
    log("[收图] n=%d -> %s" % (n, path))
    # 🔴 收图不做任何思考：不调 LLM、不猜图型、不预设用途（上一版一律当作业照片，
    #    老师发的手写课时本被回了句"发完说「生成反馈」"，图从头到尾没人看）。
    #    图先躺队列里，等下一条文本来了由理解层决定谁消费。
    rep.text("已收到图片（当前 %d 张）。需要我拿它做什么？" % n)
    return "image"


# ───────────────────────────── 飞书长连接 ─────────────────────────────


def main():
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import GetMessageResourceRequest, P2ImMessageReceiveV1

    app_id, app_secret = _env.get("FEISHU_APP_ID"), _env.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        log("缺 FEISHU_APP_ID/SECRET（%s）" % ENV_PATH)
        sys.exit(1)
    if not WHITELIST:
        log("缺 FEISHU_USER_OPENID —— 白名单为空会拒绝所有人，先补 .env")
        sys.exit(1)
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def _download_img(message_id, file_key):
        r = client.im.v1.message_resource.get(
            GetMessageResourceRequest.builder().message_id(message_id)
            .file_key(file_key).type("image").build())
        if not r.success():
            log("[取图失败] code=%s msg=%s" % (r.code, r.msg))
            return None
        f = getattr(r, "file", None)
        if f is not None:
            try:
                return f.read()
            except Exception:  # noqa: BLE001
                if isinstance(f, (bytes, bytearray)):
                    return bytes(f)
        raw = getattr(r, "raw", None)
        return raw.content if raw is not None and getattr(raw, "content", None) else None

    def on_message(data):
        """🔴 这个函数必须在毫秒内返回——它就是飞书那条 ACK。

        真正的活（理解层几秒、读图 pass 90~190 秒）全部丢后台线程，见 handle_event。
        这里只做：解析身份 → 去重 → 起线程。任何网络 IO（含取图）都在线程里。
        """
        msg = data.event.message
        sender = getattr(data.event, "sender", None)
        sid = getattr(sender, "sender_id", None) if sender else None
        open_id = getattr(sid, "open_id", None) if sid else None
        rep = LarkReplier(client, msg.message_id, msg.chat_id)
        if not open_id:
            rep.text("识别不出你的飞书身份，不能操作。")
            return
        mtype, mid, content = msg.message_type, msg.message_id, msg.content

        def work():
            if mtype == "image":
                key = json.loads(content).get("image_key", "")
                if open_id not in WHITELIST:      # 非白名单不下载、不落盘
                    handle_image(open_id, b"", mid, rep)
                    return
                data_bytes = _download_img(mid, key) if key else None
                if not data_bytes:
                    rep.text("这张图没取到，麻烦重发一次。")
                    return
                handle_image(open_id, data_bytes, mid, rep)
            elif mtype == "text":
                txt = json.loads(content).get("text", "").replace("@_user_1", "").strip()
                handle_text(open_id, txt, rep)
            else:
                if open_id in WHITELIST:
                    rep.text("我只认文字和图片。\n\n%s" % I.CAPABILITY_LIST)

        handle_event(mid, work, rep, tag=mtype or "msg")

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    log("教务速办机器人启动（PRD-016，新应用长连接；白名单 %d 人）" % len(WHITELIST))
    lark.ws.Client(app_id, app_secret, event_handler=handler,
                   log_level=lark.LogLevel.INFO).start()


def simulate(argv):
    """模拟事件注入：不连飞书，直接喂处理函数（回归自测主力）。

      --sim "文本"                     以白名单身份发一条文本
      --sim-as <openid> "文本"         指定发信人（测白名单拒绝）
      --sim-img <图路径> "文本"        先把本地图塞进队列再发文本（测⑥/⑧读图链）
    """
    if argv[0] == "--sim-as":
        open_id, texts = argv[1], argv[2:]
    elif argv[0] == "--sim-img":
        open_id = next(iter(WHITELIST)) if WHITELIST else "NO_WHITELIST"
        img_add(argv[1])
        texts = argv[2:]
    else:
        open_id, texts = (next(iter(WHITELIST)) if WHITELIST else "NO_WHITELIST"), argv[1:]
    rep = CollectReplier()
    for t in texts:
        print(">>> USER: %s" % t, flush=True)
        r = handle_text(open_id, t, rep)
        print("<<< intent=%s\n" % r, flush=True)
    return rep


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("--sim"):
        simulate(sys.argv[1:])
    else:
        main()
