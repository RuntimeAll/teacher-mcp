# -*- coding: utf-8 -*-
"""PRD-018 批4「机器人跟随」桩测 —— 纯函数级，🔴 零网络、零 BE、零 LLM。

覆盖的是这批改动里**最容易悄悄错、错了又直接错在钱上**的那几处：
  ① 小时 / 节两个单位的解析件分家（parse_hours / parse_lessons / parse_hours_per_lesson）
  ② 三档充值 body 组装（hours|lessons|amount 任一 + occurDate），不该出现的键一个都不许有
  ③ 余额话术双单位 + 共享账本只报小时
  ④ family_rebalance 退役后**不再产生任何写请求**

跑法（哪个能跑用哪个）：
  uv run pytest bots/tests/test_prd018_follow.py -q
  python bots/tests/test_prd018_follow.py          # 无 pytest 时的自断言退化跑法
"""

import os
import sys

_BOTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOTS not in sys.path:
    sys.path.insert(0, _BOTS)

import datetime  # noqa: E402

import executor as X  # noqa: E402
import intents as I  # noqa: E402
import jiaowu_bot as J  # noqa: E402

TODAY = datetime.date(2026, 8, 5)


# ───────────────────────────── ① 单位解析件分家 ─────────────────────────────


def test_parse_lessons():
    """「节 / 节课 / 次 / 课时」= 节（老师一贯的说法），带符号。"""
    assert I.parse_lessons("俊羽充 20 节") == 20
    assert I.parse_lessons("俊羽充 20 节课") == 20
    assert I.parse_lessons("俊羽调整 -1 节 备注 补扣") == -1
    assert I.parse_lessons("充 3 次") == 3
    # 🔴 「350 一节」里的「一节」是单价量词，不剥会把 20 节读成 1 节（历史真 bug）
    assert I.parse_lessons("350 一节充 20 节") == 20
    # 🔴 「一节 1.5 小时」是每节时长，不是「1 节」
    assert I.parse_lessons("给俊羽开个数学户 350 一节，一节 1.5 小时") is None
    assert I.parse_lessons("充 3 小时") is None


def test_parse_hours():
    """只认小时：节/课时/次一律不进这里（PRD-018 底账单位）。"""
    assert I.parse_hours("充 3 小时") == 3
    assert I.parse_hours("充 30 个小时") == 30
    assert I.parse_hours("俊羽充 2h") == 2
    assert I.parse_hours("俊羽充 -2 小时") == -2
    # 🔴 换轨核心断言：「20 节」绝不能再被读成 20 小时
    assert I.parse_hours("俊羽充 20 节") is None
    assert I.parse_hours("350 一节充 20 节") is None
    # 「一节 1.5 小时」是计价口径，不是一笔 1.5 小时的充值
    assert I.parse_hours("给俊羽开个数学户 350 一节，一节 1.5 小时") is None


def test_parse_hours_per_lesson():
    assert I.parse_hours_per_lesson("一节 1.5 小时") == 1.5
    assert I.parse_hours_per_lesson("每节1.5小时") == 1.5
    assert I.parse_hours_per_lesson("一节课一个半小时") == 1.5
    assert I.parse_hours_per_lesson("1.5 小时算一节") == 1.5
    assert I.parse_hours_per_lesson("给俊羽开个数学户 350 一节，一节 2 个小时") == 2
    assert I.parse_hours_per_lesson("俊羽充 20 节") is None


def test_parse_settle_hours_unchanged():
    """结算量语义不动：入参 hours 本来就是小时。"""
    assert I.parse_settle_hours("按 1.5 课时扣") == 1.5
    assert I.parse_settle_hours("每场扣 2 节") == 2
    assert I.parse_settle_hours("把待办清了") is None
    # 🔴 「按 1.5 课时扣」被读成「单价 1.5 元」= 开户预览出荒唐价（改 parse_price 时抓到）
    assert I.parse_price("按 1.5 课时扣") is None
    assert I.parse_price("把俊羽时薪改成 260") == 260
    assert I.parse_price("350 一节") == 350


def test_extract_new_keys():
    e = I.extract("350 一节充 20 节", [], TODAY)
    for k in ("hours", "lessons", "hoursPerLesson", "amount", "price"):
        assert k in e, "extract 缺键 %s" % k
    assert e["lessons"] == 20 and e["hours"] is None and e["price"] == 350

    e2 = I.extract("给俊羽开个数学户 350 一节，一节 1.5 小时", [], TODAY)
    assert e2["hoursPerLesson"] == 1.5 and e2["lessons"] is None and e2["hours"] is None

    # 结算量认掉之后不再往 lessons 塞一份（同一句话被两档同时读走 = 预览自相矛盾）
    e3 = I.extract("按 1.5 课时扣", [], TODAY)
    assert e3["hours"] == 1.5 and e3["lessons"] is None


# ───────────────────────── ② add_flow body 组装（三档 + occurDate） ─────────────────────────


class _CapturingBe(X.Be):
    """只截 call()，不发任何请求（🔴 桩测绝不连 BE）。"""

    def __init__(self):
        X.Be.__init__(self, "openid-stub", "secret-stub", "client-stub")
        self.calls = []

    def call(self, path, body=None, method=None, retry=True):
        self.calls.append({"path": path, "body": body, "method": method})
        return {"id": "9001"}


def _flow_body(**kw):
    be = _CapturingBe()
    be.add_flow("60", "1", **kw)
    assert be.calls[0]["path"] == "/teacher/schedule/account/60/flow"
    return be.calls[0]["body"]


def test_add_flow_three_tiers():
    assert _flow_body(hours=30) == {"flowType": "1", "hours": 30}
    assert _flow_body(lessons=20) == {"flowType": "1", "lessons": 20}
    assert _flow_body(amount=7000) == {"flowType": "1", "amount": 7000}


def test_add_flow_occur_date_and_note():
    b = _flow_body(lessons=10, occur_date="2026-04-12", note="课时本补录")
    assert b == {"flowType": "1", "lessons": 10, "occurDate": "2026-04-12", "note": "课时本补录"}


def test_add_flow_empty_body_has_no_extra_keys():
    """一档都不给 → body 只剩 flowType（零值键不许漏进去，BE 会当成给了那一档）。"""
    assert _flow_body() == {"flowType": "1"}
    assert _flow_body(hours=0, lessons=0, amount=0, occur_date=None, note=None) == {"flowType": "1"}


def test_upsert_account_body():
    be = _CapturingBe()
    be.upsert_account("60", "数学", 233.3333, 1.5, note="俊羽家")
    b = be.calls[0]["body"]
    assert b["pricePerHour"] == 233.3333 and b["hoursPerLesson"] == 1.5
    assert b["subject"] == "1" and b["studentId"] == "60" and b["note"] == "俊羽家"
    assert "lessonPrice" not in b        # 旧口径不许再往外发


# ───────────────────────── ③ 余额话术 / 档位话术 ─────────────────────────

_ACC_SOLO = {"id": "60", "hoursRemain": 3.0, "hoursPerLesson": 1.5, "lessonsRemain": 2.0,
             "pricePerHour": 233.3333, "lessonPrice": 350.0, "shared": False, "status": "0"}
_ACC_SHARED = dict(_ACC_SOLO, id="61", shared=True)


def test_bal_text_dual_unit():
    assert X.bal_text(_ACC_SOLO) == "3 小时（≈2 节）"
    # 🔴 共享账本每人每节时长不同 → 只报小时（D4 规则②）
    assert X.bal_text(_ACC_SHARED) == "3 小时"
    assert X.is_shared(_ACC_SHARED) is True and X.is_shared(_ACC_SOLO) is False
    # 账本视角 VO 用的是 bindingCount（曾经写成不存在的 bindCount，共享判定永远为假）
    assert X.is_shared({"bindingCount": 2}) is True


def test_flow_brief_tier_priority_and_sign():
    assert X.flow_brief({"hours": 30}) == "+30 小时"
    assert X.flow_brief({"lessons": 20}) == "+20 节"
    assert X.flow_brief({"amount": 7000}) == "+7000 元"
    assert X.flow_brief({"lessons": -1}) == "-1 节"


def test_tier_args_shared_book_converts_lessons():
    """共享账本 BE 明确拒收「节」档 → 机器人按本人每节时长先折成小时，并在预览里说明。"""
    tier, hint = J._tier_args(_ACC_SHARED, None, 20, None)
    assert tier == {"hours": 30.0, "lessons": None, "amount": None}
    assert hint and "共享账本" in hint
    tier2, hint2 = J._tier_args(_ACC_SOLO, None, 20, None)
    assert tier2 == {"hours": None, "lessons": 20, "amount": None} and hint2 is None


def test_price_per_hour():
    """「350 一节」+ 每节 1.5 小时 → 时薪 233.3333；明说时薪则原样收。"""
    assert J._price_per_hour(350, 1.5, "开个户 350 一节，一节 1.5 小时") == (233.3333, False)
    assert J._price_per_hour(260, 1.5, "把俊羽时薪改成 260") == (260.0, True)
    assert J._price_per_hour(None, 1.5, "开个户") == (None, False)


def test_planned_hours_from_pending_row():
    """结算默认扣多少只认 BE 的 plannedHours（自己定 1.0 = 每节少扣三分之一）。"""
    assert J._planned_hours({"plannedHours": 1.5}) == 1.5
    assert J._planned_hours({"plannedHours": None}) == 1.0
    assert J._planned_hours({}) == 1.0


def test_delta_hours_preview_only():
    assert J._delta_hours(_ACC_SOLO, 3, None, None) == 3.0
    assert J._delta_hours(_ACC_SOLO, None, 20, None) == 30.0
    assert round(J._delta_hours(_ACC_SOLO, None, None, 7000), 2) == 30.0
    assert J._delta_hours({}, None, 20, None) is None


# ───────────────────────── ④ family_rebalance 退役 ─────────────────────────


class _ExplodingBe(X.Be):
    """任何一次 call = 测试失败（用来证明这条路径真的一个请求都不发）。"""

    def __init__(self):
        X.Be.__init__(self, "openid-stub", "secret-stub", "client-stub")

    def call(self, path, body=None, method=None, retry=True):
        raise AssertionError("family_rebalance 已退役，不该再调 BE：%s" % path)


def test_family_books_dedup_shared_account():
    """🔴 并本之后最容易出的错：按学生逐个相加 → 同一本账数两遍，家里余额凭空翻倍。"""
    members = [({"id": "60", "name": "俊羽"}, _ACC_SHARED),
               ({"id": "59", "name": "好好"}, _ACC_SHARED)]
    books = J._family_books(members)
    assert len(books) == 1 and books[0][1] == ["俊羽", "好好"]
    assert J._family_hours(members) == 3.0        # 不是 6.0
    # 各绑各本时照常相加
    solo = [({"id": "60", "name": "俊羽"}, _ACC_SOLO),
            ({"id": "59", "name": "好好"}, dict(_ACC_SOLO, id="62", hoursRemain=-5.0))]
    assert len(J._family_books(solo)) == 2 and J._family_hours(solo) == -2.0


def test_family_rebalance_retired():
    r = X.run_op(_ExplodingBe(), "family_rebalance", {})
    assert r["image_path"] is None
    assert "共享账" in r["text"] and "PRD-018" in r["text"]
    # be=None 也要能跑（jiaowu_bot.act_family_rebalance 就是这么调的，连 BE 都不建）
    assert X.run_op(None, "family_rebalance", {})["text"] == r["text"]


def test_ingest_settle_items_carry_content():
    """⑧ 补录：课时本的「上课内容」要随结算写进 session.content（D5/G7）。"""
    calls = []

    class _Be(X.Be):
        def __init__(self):
            X.Be.__init__(self, "o", "s", "c")

        def session_batch(self, target_id, items, plan_id=None, force=True):
            return {"created": [{"id": "701", "sessionDate": "2026-04-12", "startTime": "13:30:00"}]}

        def settle(self, items, gen_feedback=False):
            calls.append(items)
            return {"settled": 1, "skipped": []}

        def add_flow(self, *a, **kw):
            calls.append(("flow", a, kw))
            return {"id": "1"}

        def roster(self, force=False):
            return []

        def export_ledger_png(self, account_id):
            raise X.BeError("stub 不出图")

    args = {"studentId": "60", "studentName": "俊羽", "subject": "数学", "accountId": "60",
            "items": [{"date": "2026-04-12", "start": "13:30", "end": "15:00"}],
            "hoursByKey": {"2026-04-12 13:30": 1.5},
            "contentByKey": {"2026-04-12 13:30": "数字迷、图形面积"},
            "expectAmount": 350.0, "recharges": []}
    out = X.run_op(_Be(), "ingest_lesson_log", args)
    assert calls[0] == [{"sessionId": "701", "hours": 1.5, "content": "数字迷、图形面积"}]
    assert "1.5 小时" in out["text"]


# ───────────────────────────── 退化跑法（无 pytest 时） ─────────────────────────────

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # 🔴 GBK 控制台会杀掉带中文/emoji 的 print
    except Exception:
        pass
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS  %s" % name)
        except Exception as e:                       # noqa: BLE001
            bad += 1
            print("FAIL  %s -> %r" % (name, e))
    print("\n%d passed, %d failed（共 %d）" % (len(fns) - bad, bad, len(fns)))
    sys.exit(1 if bad else 0)
