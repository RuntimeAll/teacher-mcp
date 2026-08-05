# -*- coding: utf-8 -*-
"""PRD-018 批4「机器人真机终验」—— bots/executor.py 的 Be 层直连 B 位 dev BE(:9290) 冒烟。

和同目录 test_prd018_follow.py 的分工：
  · test_prd018_follow.py = 纯函数桩测（零网络），管「话术/解析/body 组装」不出错；
  · 本脚本                = 真机链路（真 BE + 真 MySQL），管「Be 层发出去的请求 BE 真认、
                            算出来的数真落库」——桩测替不了的那一半。

🔴 环境铁律（写死，勿改）：
  · BE 只连 http://localhost:9290（B 位 dev），MySQL 只连 127.0.0.1:3307；**绝不碰 prod**。
  · 凭据从 teacher-mcp/.env 读（RUOYI_USERNAME/PASSWORD/CLIENT_ID/TENANT_ID），不硬编码。
    🔴 .env 里的 RUOYI_BASE_URL 指的是 O 位 :9390，本脚本**不用它**，base 硬指 9290。
  · 只读/新建自己的数据，测试数据一律 `PRD018BOT` 前缀，跑完清零并复核残留。

为什么要子类化 Be 而不是塞 _tok：
  Be.call() 撞 401/403 会自动 token(force=True) 回落 /auth/botLogin（bot 凭据本地没有，
  且 botLogin 是 prod 那条链）—— 会把注入的 admin token 冲掉。所以覆写 token() 直接返回。

跑法（pyproject 的 testpaths 不收 bots/tests，故写成直跑脚本）：
    python bots/tests/live_smoke_9290.py
    python bots/tests/live_smoke_9290.py > bots/tests/live_smoke_9290.log 2>&1
"""

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")   # 🔴 GBK 控制台遇中文/emoji 会直接杀进程

_BOTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOTS not in sys.path:
    sys.path.insert(0, _BOTS)
_ROOT = os.path.dirname(_BOTS)

# 🔴 只 import executor：jiaowu_bot 在 import 期读 /opt/jiaowu-push/.env 且强制要 bot secret
import executor as X  # noqa: E402

BASE = "http://localhost:9290"
X.RUOYI = BASE          # 模块级常量，_raw/download 按名字读全局 → 赋值即生效（末尾不带 /）

ENV = X.load_env(os.path.join(_ROOT, ".env"))
CLIENT_ID = ENV.get("RUOYI_CLIENT_ID") or "e5cd7e4891bf95d1d19206ce24a7b32e"
TENANT_ID = ENV.get("RUOYI_TENANT_ID") or "000000"
USERNAME = ENV.get("RUOYI_USERNAME") or "admin"
PASSWORD = ENV.get("RUOYI_PASSWORD") or "admin123"

DB = dict(host="127.0.0.1", port=3307, user="root", password="123456",
          database="ai_lesson_prep", charset="utf8mb4")

PREFIX = "PRD018BOT"
TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)
DAY_BEFORE = TODAY - datetime.timedelta(days=2)
HIST_DATE = "2026-04-12"          # 补录历史充值用的业务日期

PRICE_PER_HOUR = 233.3333         # 4 位小数（列是 decimal(10,4)）→ 验金额换算不被精度吃掉
HOURS_PER_LESSON = 1.5

FAILS = []
CREATED = {"students": [], "accounts": [], "sessions": []}


# ───────────────────────────── 基础件 ─────────────────────────────


def sec(title):
    print("\n" + "=" * 72)
    print("=== " + title)
    print("=" * 72)


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("  [%s] %s%s" % (tag, name, ("  " + detail) if detail else ""))


def near(a, b, eps=0.005):
    return a is not None and b is not None and abs(float(a) - float(b)) < eps


def db():
    import pymysql
    return pymysql.connect(**DB)


def q1(sql, args=()):
    c = db()
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        c.close()


def qall(sql, args=()):
    c = db()
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        c.close()


def admin_token():
    """自己 POST /auth/login 拿 admin token（/auth/** 是 code==200 制式）。"""
    body = {"clientId": CLIENT_ID, "grantType": "password", "tenantId": TENANT_ID,
            "username": USERNAME, "password": PASSWORD}
    req = urllib.request.Request(BASE + "/auth/login",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("clientid", CLIENT_ID)
    with X.OPENER.open(req, timeout=30) as r:      # OPENER 已 ProxyHandler({})，localhost 不吃代理
        d = json.loads(r.read().decode("utf-8"))
    if d.get("code") != 200 or not (d.get("data") or {}).get("access_token"):
        raise RuntimeError("admin 登录失败：%s" % json.dumps(d, ensure_ascii=False)[:300])
    return d["data"]["access_token"]


class LiveBe(X.Be):
    """真机 Be：走真实 Be 的每一个方法与 envelope 解包，只把签发换成注入的 admin token。"""

    def __init__(self, token, client_id):
        X.Be.__init__(self, "n/a", "n/a", client_id)
        self._tok = token
        self._tok_ts = time.time()

    def token(self, force=False):
        return self._tok       # 🔴 永不回落 botLogin（那条链是 prod 的）


def mk_student(be, name):
    r = be.call("/teacher/schedule/target",
                {"targetType": "0", "name": name, "gradeNo": 7,
                 "gradeYear": 2026, "subject": "1"})
    sid = r["id"]
    CREATED["students"].append(sid)
    print("    建学生 %s → id=%s" % (name, sid))
    return sid


def acc_of_roster(be, student_id, subject="数学"):
    """走 Be.roster + Be.account_of 拿学生视角账户 VO（bal_text 的真实入参形态）。"""
    be.roster(force=True)
    for st in be.roster():
        if str(st.get("id")) == str(student_id):
            return be.account_of(st, subject)
    return None


# ═════════════════════ ① 开户（时薪 + 每节时长落库） ═════════════════════


def step1_open(be):
    sec("① 开户：Be.upsert_account → 时薪 233.3333 元/小时 · 每节 1.5 小时")
    sid = mk_student(be, PREFIX + "俊羽")
    r = be.upsert_account(sid, "数学", PRICE_PER_HOUR, HOURS_PER_LESSON,
                          note=PREFIX + " 真机开户")
    print("    upsert_account 返回：%s" % json.dumps(r, ensure_ascii=False)[:300])
    acc = (r or {}).get("id")
    check("1-a upsert_account 返回 accountId", bool(acc), "accountId=%s" % acc)
    if not acc:
        raise RuntimeError("开户没拿到 accountId，后续无从跑起")
    CREATED["accounts"].append(acc)

    price = q1("SELECT price_per_hour FROM biz_tuition_account WHERE id=%s", (acc,))
    check("1-b 账本 price_per_hour = 233.3333（4 位小数没被吃掉）", near(price, PRICE_PER_HOUR, 1e-6),
          "实测 %s" % price)
    row = qall("SELECT account_id, hours_per_lesson FROM biz_student_account_link "
               "WHERE student_id=%s AND subject='1'", (sid,))
    check("1-c 该生数学绑定唯一且指向本账本", len(row) == 1 and str(row[0][0]) == str(acc),
          "link=%s" % (row,))
    if row:
        check("1-d 绑定 hours_per_lesson = 1.5", near(row[0][1], HOURS_PER_LESSON),
              "实测 %s" % row[0][1])
    return sid, acc


# ═════════════════════ ② 三档充值（节 / 小时 / 元 + 补录历史） ═════════════════════


def step2_flows(be, acc):
    sec("② 三档充值各一笔：20 节 / 3 小时 / 700 元（其中「3 小时」那笔补录到 %s）" % HIST_DATE)
    be.add_flow(acc, "1", lessons=20, note=PREFIX + " 按节充值")
    be.add_flow(acc, "1", hours=3, occur_date=HIST_DATE, note=PREFIX + " 按小时补录")
    be.add_flow(acc, "1", amount=700, note=PREFIX + " 按金额充值")

    rows = qall("SELECT id, flow_type, hours_delta, amount_paid, occur_date, note "
                "FROM biz_tuition_flow WHERE account_id=%s ORDER BY id", (acc,))
    print("    落库流水（按 id 序）：")
    for r in rows:
        print("      flow_type=%s hours_delta=%s amount_paid=%s occur_date=%s note=%s"
              % (r[1], r[2], r[3], r[4], r[5]))
    check("2-0 三笔充值全部落库", len(rows) == 3, "实测 %d 行" % len(rows))
    if len(rows) != 3:
        return
    by_note = {r[5]: r for r in rows}
    f_les = by_note.get(PREFIX + " 按节充值")
    f_hrs = by_note.get(PREFIX + " 按小时补录")
    f_amt = by_note.get(PREFIX + " 按金额充值")

    # ---- 换算口径（BE resolveHours：hours → lessons → amount，一律 scale2 HALF_UP）----
    print("    换算口径复核：20 节 × 1.5 h/节 = 30 h ｜ 3 h 原样 ｜ 700 ÷ 233.3333 = %.6f → scale2"
          % (700 / PRICE_PER_HOUR))
    check("2-a 按节：hours_delta = 20 × 1.5 = 30.00", near(f_les[2], 30), "实测 %s" % f_les[2])
    check("2-b 按小时：hours_delta = 3.00（原样）", near(f_hrs[2], 3), "实测 %s" % f_hrs[2])
    check("2-c 按金额：hours_delta = 700 ÷ 233.3333 = 3.00", near(f_amt[2], 3), "实测 %s" % f_amt[2])

    # ---- amount_paid 口径：BE = amountPaid ?? amount（按节/按小时那两档不派生金额，留 NULL）----
    check("2-d 按金额那笔 amount_paid = 700.00（入参原样）", near(f_amt[3], 700), "实测 %s" % f_amt[3])
    check("2-e 按节那笔 amount_paid 为空（BE 不替它派生金额）", f_les[3] is None, "实测 %s" % f_les[3])
    check("2-f 按小时那笔 amount_paid 为空", f_hrs[3] is None, "实测 %s" % f_hrs[3])

    # ---- occur_date：补录走入参，其余落今天 ----
    check("2-g 补录那笔 occur_date = %s（历史日期回填生效）" % HIST_DATE,
          str(f_hrs[4]) == HIST_DATE, "实测 %s" % f_hrs[4])
    check("2-h 未传 occurDate 的两笔 occur_date = 今天 %s" % TODAY,
          str(f_les[4]) == str(TODAY) and str(f_amt[4]) == str(TODAY),
          "实测 %s / %s" % (f_les[4], f_amt[4]))

    total = q1("SELECT hours_remain FROM biz_tuition_account WHERE id=%s", (acc,))
    check("2-i 账本 hours_remain = 30 + 3 + 3 = 36.00", near(total, 36), "实测 %s" % total)


# ═════════════════════ ③ 台账 + 余额话术（双单位） ═════════════════════


def step3_ledger(be, sid, acc):
    sec("③ 台账与余额话术：Be.ledger + bal_text / _bal_line")
    lg = be.ledger(acc, page_size=50)
    rows = lg.get("rows") or []
    print("    台账 %d 行（按业务日期正序）：" % len(rows))
    for r in rows:
        print("      %s type=%s hours=%s 剩余=%s content=%r"
              % (r.get("date"), r.get("flowType"), r.get("hoursDelta"),
                 r.get("hoursAfter"), r.get("content")))
    check("3-a 台账 3 行（三笔充值）", len(rows) == 3, "实测 %d" % len(rows))
    check("3-b 首行 = 补录的历史日期 %s（台账按业务日期排序，不按录入序）" % HIST_DATE,
          bool(rows) and rows[0].get("date") == HIST_DATE,
          "实测 %s" % (rows[0].get("date") if rows else None))
    last_after = rows[-1].get("hoursAfter") if rows else None
    check("3-c 台账末行剩余 = 36.00", near(last_after, 36), "实测 %s" % last_after)

    a = acc_of_roster(be, sid)
    check("3-d Be.roster + account_of 能取到该生数学账户 VO", bool(a),
          "keys=%s" % (sorted(a.keys()) if a else None))
    if not a:
        return
    print("    账户 VO：hoursRemain=%s lessonsRemain=%s hoursPerLesson=%s shared=%s bindingCount=%s"
          % (a.get("hoursRemain"), a.get("lessonsRemain"), a.get("hoursPerLesson"),
             a.get("shared"), a.get("bindingCount")))
    check("3-e is_shared = False（单绑本）", X.is_shared(a) is False, "实测 %s" % X.is_shared(a))

    txt = X.bal_text(a)
    line = X._bal_line(a)
    print("    bal_text  → %r" % txt)
    print("    _bal_line → %r" % line)
    check("3-f 话术含「小时」（小时为底账单位）", "小时" in txt, "实测 %r" % txt)
    check("3-g 单绑本带折节副显「≈N 节」", ("≈" in txt and "节" in txt), "实测 %r" % txt)
    check("3-h 话术 = 「36 小时（≈24 节）」（36 ÷ 1.5 = 24）", txt == "36 小时（≈24 节）",
          "实测 %r" % txt)
    check("3-i _bal_line = 📊 前缀 + bal_text", line == "📊 当前余额：" + txt, "实测 %r" % line)
    check("3-j 话术数值 == DB hours_remain == 台账末行剩余",
          near(a.get("hoursRemain"), 36) and near(last_after, 36)
          and near(q1("SELECT hours_remain FROM biz_tuition_account WHERE id=%s", (acc,)), 36),
          "VO=%s 台账末行=%s" % (a.get("hoursRemain"), last_after))


# ═════════════════════ ④ 待办 plannedHours（= 每节时长，不是场次时长） ═════════════════════


def step4_pending(be, sid):
    sec("④ 待办：Be.session_batch 排两场（时长 1.5h / 2h）→ Be.pending 的 plannedHours 都应 = 1.5")
    items = [
        {"date": str(YESTERDAY), "start": "09:00", "end": "10:30",
         "sessionType": "1", "subject": "1"},          # 场次时长 1.5h（与每节时长巧合）
        {"date": str(DAY_BEFORE), "start": "09:00", "end": "11:00",
         "sessionType": "1", "subject": "1"},          # 🔴 场次时长 2h —— 用来证明不是巧合
    ]
    r = be.session_batch(sid, items)
    created = r.get("created") or []
    print("    session_batch created=%d conflicts=%s"
          % (len(created), json.dumps(r.get("conflicts") or [], ensure_ascii=False)[:200]))
    by_date = {}
    for c in created:
        by_date[str(c.get("sessionDate"))] = c["id"]
        CREATED["sessions"].append(c["id"])
    check("4-a 两场都建出来了", len(created) == 2, "实测 %d" % len(created))
    s_15 = by_date.get(str(YESTERDAY))
    s_20 = by_date.get(str(DAY_BEFORE))
    if not (s_15 and s_20):
        raise RuntimeError("排课没拿全 sessionId：%s" % by_date)
    print("    昨天(09:00-10:30, 1.5h) session=%s ｜ 前天(09:00-11:00, 2h) session=%s" % (s_15, s_20))

    pend = be.pending()
    mine = {p["sessionId"]: p for p in pend if p["sessionId"] in (str(s_15), str(s_20))}
    print("    待结算清单共 %d 条，本次命中 %d 条：" % (len(pend), len(mine)))
    for k, p in mine.items():
        print("      session=%s date=%s %s-%s plannedHours=%s hoursPerLesson=%s "
              "plannedAmount=%s content=%r sessionStatus=%s"
              % (k, p.get("date"), p.get("start"), p.get("end"), p.get("plannedHours"),
                 p.get("hoursPerLesson"), p.get("plannedAmount"), p.get("content"),
                 p.get("sessionStatus")))
    check("4-b 两场都在 pending 清单里", len(mine) == 2, "命中 %d" % len(mine))
    p15, p20 = mine.get(str(s_15)), mine.get(str(s_20))
    if p15:
        check("4-c 1.5h 场次 plannedHours = 1.5", near(p15.get("plannedHours"), 1.5),
              "实测 %s" % p15.get("plannedHours"))
    if p20:
        check("4-d 🔴 2h 场次 plannedHours 仍 = 1.5（吃每节时长，不是场次起止时长——排除巧合）",
              near(p20.get("plannedHours"), 1.5), "实测 %s" % p20.get("plannedHours"))
        check("4-e plannedAmount = 1.5 × 233.3333 = 350.00",
              near(p20.get("plannedAmount"), 350), "实测 %s" % p20.get("plannedAmount"))
        check("4-f 批4 新增 content 字段存在且此刻为空（结算前没写过）",
              "content" in p20 and not p20.get("content"), "实测 %r" % p20.get("content"))
    return s_15, s_20


# ═════════════════════ ⑤ 结算（吃 plannedHours 默认 + 写 content） ═════════════════════


def step5_settle(be, acc, s_15):
    sec("⑤ 结算：Be.settle 不传 hours（吃 plannedHours 默认）+ 顺手写「这节讲了什么」")
    content = PREFIX + " 真机结算"
    bal_before = float(q1("SELECT hours_remain FROM biz_tuition_account WHERE id=%s", (acc,)))
    r = be.settle([{"sessionId": str(s_15), "content": content}])
    print("    settle 返回：%s" % json.dumps(r, ensure_ascii=False)[:400])
    check("5-a settled >= 1", (r or {}).get("settled", 0) >= 1,
          "settled=%s skipped=%s" % ((r or {}).get("settled"), (r or {}).get("skipped")))

    row = qall("SELECT session_status, settle_status, content, session_date "
               "FROM biz_schedule_session WHERE id=%s", (s_15,))[0]
    check("5-b 场次 session_status='1'（已上）", row[0] == "1", "实测 %s" % row[0])
    check("5-c 场次 settle_status='1'（已结）", row[1] == "1", "实测 %s" % row[1])
    check("5-d 场次 content 已写入", row[2] == content, "实测 %r" % row[2])

    fl = qall("SELECT flow_type, hours_delta, amount_paid, occur_date FROM biz_tuition_flow "
              "WHERE account_id=%s AND session_id=%s ORDER BY id", (acc, s_15))
    print("    扣课流水：%s" % (fl,))
    check("5-e 新增 1 条扣课流水（flow_type='2'）", len(fl) == 1 and fl[0][0] == "2",
          "实测 %s" % (fl,))
    if fl:
        check("5-f 扣课 hours_delta = -1.50（= plannedHours，非场次时长）",
              near(fl[0][1], -1.5), "实测 %s" % fl[0][1])
        check("5-g 扣课 occur_date = 场次日期 %s" % YESTERDAY,
              str(fl[0][3]) == str(YESTERDAY), "实测 %s" % fl[0][3])
        check("5-h 扣课 amount_paid = -(1.5 × 233.3333) = -350.00", near(fl[0][2], -350),
              "实测 %s" % fl[0][2])

    bal_after = float(q1("SELECT hours_remain FROM biz_tuition_account WHERE id=%s", (acc,)))
    check("5-i 账户余额 %s → %s（正好减 1.5）" % (bal_before, bal_after),
          near(bal_after, bal_before - 1.5), "实测 %s" % bal_after)

    # 结算后再看一眼待办与话术（机器人真实的下一句话）
    pend_ids = [p["sessionId"] for p in be.pending()]
    check("5-j 已结场次退出待结算清单", str(s_15) not in pend_ids)
    a = acc_of_roster(be, CREATED["students"][0])
    txt = X.bal_text(a)
    print("    结算后 bal_text → %r" % txt)
    check("5-k 结算后话术 = 「34.5 小时（≈23 节）」（34.5 ÷ 1.5 = 23）",
          txt == "34.5 小时（≈23 节）", "实测 %r" % txt)


# ───────────────────────────── 清理 ─────────────────────────────


def cleanup():
    sec("清零：删掉本次 PRD018BOT 建的全部数据并复核残留")
    c = db()
    try:
        cur = c.cursor()
        for a in CREATED["accounts"]:
            cur.execute("DELETE FROM biz_tuition_flow WHERE account_id=%s", (a,))
            cur.execute("DELETE FROM biz_student_account_link WHERE account_id=%s", (a,))
            cur.execute("DELETE FROM biz_tuition_account WHERE id=%s", (a,))
        for s in CREATED["sessions"]:
            cur.execute("DELETE FROM biz_schedule_session WHERE id=%s", (s,))
        for stu in CREATED["students"]:
            cur.execute("DELETE FROM biz_student_account_link WHERE student_id=%s", (stu,))
            cur.execute("DELETE FROM biz_schedule_session WHERE target_id=%s", (stu,))
            cur.execute("DELETE FROM biz_feedback_sheet WHERE target_id=%s", (stu,))
            cur.execute("DELETE FROM biz_student WHERE id=%s AND name LIKE %s",
                        (stu, PREFIX + "%"))
        c.commit()
    finally:
        c.close()

    def cnt(sql, ids):
        if not ids:
            return 0
        marks = ",".join(["%s"] * len(ids))
        return q1(sql % marks, tuple(ids)) or 0

    left = {
        "学生(name LIKE PRD018BOT%)": q1("SELECT COUNT(*) FROM biz_student WHERE name LIKE %s",
                                        (PREFIX + "%",)),
        "账本": cnt("SELECT COUNT(*) FROM biz_tuition_account WHERE id IN (%s)", CREATED["accounts"]),
        "绑定link": cnt("SELECT COUNT(*) FROM biz_student_account_link WHERE account_id IN (%s)",
                      CREATED["accounts"]),
        "流水": cnt("SELECT COUNT(*) FROM biz_tuition_flow WHERE account_id IN (%s)",
                  CREATED["accounts"]),
        "场次": cnt("SELECT COUNT(*) FROM biz_schedule_session WHERE id IN (%s)", CREATED["sessions"]),
        "场次(按学生)": cnt("SELECT COUNT(*) FROM biz_schedule_session WHERE target_id IN (%s)",
                       CREATED["students"]),
    }
    print("  残留复核（都应为 0）：" + " ｜ ".join("%s=%s" % (k, v) for k, v in left.items()))
    check("Z-a 测试数据清零", all(v == 0 for v in left.values()), str(left))


# ───────────────────────────── main ─────────────────────────────


def main():
    rc = 0
    print("PRD-018 批4 · 机器人真机终验（bots/executor.py Be 层 → %s）" % BASE)
    print("时间：%s ｜ 今天=%s 昨天=%s 前天=%s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                          TODAY, YESTERDAY, DAY_BEFORE))
    print("凭据来源：%s（用户=%s，client_id=%s…）" % (os.path.join(_ROOT, ".env"),
                                            USERNAME, CLIENT_ID[:8]))
    try:
        be = LiveBe(admin_token(), CLIENT_ID)
        print("admin token 注入完成（Be.token 已覆写，绝不回落 botLogin）")
        sid, acc = step1_open(be)
        step2_flows(be, acc)
        step3_ledger(be, sid, acc)
        s_15, _s_20 = step4_pending(be, sid)
        step5_settle(be, acc, s_15)
    except Exception as e:
        print("\n!!! 冒烟中断：%r" % (e,))
        import traceback
        traceback.print_exc()
        rc = 2
    finally:
        try:
            cleanup()
        except Exception as e:
            print("清理失败（需人工核）：%r" % (e,))
            rc = rc or 2

    print("\n" + "=" * 72)
    if FAILS:
        print("结论：FAIL —— %d 条断言未过：%s" % (len(FAILS), FAILS))
        rc = rc or 1
    elif rc == 0:
        print("结论：PASS —— 五步真机链路（开户 / 三档充值 / 台账话术 / 待办 plannedHours / 结算）全绿")
    return rc


if __name__ == "__main__":
    sys.exit(main())
