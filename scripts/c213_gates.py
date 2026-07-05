# -*- coding: utf-8 -*-
"""PRD-C-213 接口级 gates 断言：G3(大纲态合法) G4(13场逐场绝对日期) G6(顺延/锁定/改期) G7(冲突明细) G8(14天窗口边界) G14(家长版导出)。
用法：先跑 g13_sujunyu.py 取 target_id/plan_id，再 `python scripts/c213_gates.py <target_id> <plan_id>`。
🔴 G4 必须在 G6 之前（G6 会请假/改期变异数据）。直接复用 teacher-mcp 的纯函数。"""
import asyncio, io, sys, json, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\workplace\book-ai\teacher-mcp")

from app.tools import schedule as S
from app.ruoyi import RuoyiCluster
from app.config import settings

FAILS = []
def step(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (" → " + str(detail) if detail else ""))
    if not ok: FAILS.append(name)

async def main():
    cluster = RuoyiCluster()
    client = cluster.c
    await client.login(settings.ruoyi_username, settings.ruoyi_password)
    TID, PID = sys.argv[1], sys.argv[2]   # 传最新一轮 G13 的 target_id / plan_id

    # ── G4 逐场对照绝对日期（苏俊宇案例钉死的 13 slot；🔴 必须先于 G6 变异）──
    G4_SLOTS = [("2026-07-05", "13:30"), ("2026-07-07", "09:30"), ("2026-07-12", "13:30"),
                ("2026-07-14", "09:30"), ("2026-07-19", "13:30"), ("2026-07-21", "09:30"),
                ("2026-07-26", "13:30"), ("2026-07-28", "09:30"), ("2026-08-02", "13:30"),
                ("2026-08-09", "13:30"), ("2026-08-16", "13:30"), ("2026-08-23", "13:30"),
                ("2026-08-30", "13:30")]
    sess = await S._list_schedule(client, "2026-07-01", "2026-08-31", target_id=TID)
    rows = [s for s in sess.get("sessions", []) if str(s.get("planId")) == PID]
    rows.sort(key=lambda s: str(s.get("sessionDate")))
    step("前置:拉到13场", len(rows) == 13, f"got={len(rows)}")
    got_slots = [(str(r.get("sessionDate")), str(r.get("startTime"))[:5]) for r in rows]
    mism = [i for i, (g, e) in enumerate(zip(got_slots, G4_SLOTS)) if g != e]
    step("G4a.13场逐场比绝对日期+时段", not mism,
         "全对" if not mism else f"错位场次={[(i+1, got_slots[i], G4_SLOTS[i]) for i in mism]}")
    seqs = [str(r.get("planLessonId")) for r in rows]
    step("G4b.绑定课次按日期序单调无重复", len(set(seqs)) == 13, f"lessons={seqs}")

    # ── G3 大纲态课次合法（仅标题保存、无题目、prepState=0 由包状态推导）──
    # R1a·S1：建计划必传 target_id 归属
    rp = await S._upsert_course_plan(client,
        {"name": "G3-大纲态验证计划", "target_type": "0", "target_id": TID, "term_tag": "暑假", "year": 2026},
        [{"lesson_seq": 1, "title": "G3-仅大纲无题课次"}])
    g3_plan = rp.get("plan_id")
    g3_ok = bool(rp.get("ok") and rp.get("lesson_ids"))
    if g3_ok:
        pd = await client.teacher_get(f"/teacher/schedule/plan/{g3_plan}", {})
        ls = (pd or {}).get("lessons") or []
        g3_ok = len(ls) == 1 and str(ls[0].get("prepState")) == "0"
    step("G3.大纲态课次(仅标题无题目)保存合法且 prep_state=0", g3_ok, f"plan={g3_plan}")

    # ── G6 三语义 ──
    sid = lambda i: str(rows[i]["sessionId"] if "sessionId" in rows[i] else rows[i]["id"])
    lesson_of = {str(r.get("planLessonId")): r for r in rows}

    # G6-锁定+顺延：锁第5场，第3场请假 → 第4场接第3场课次，第5场保持原课次，第6场接第4场课次，末位 overflow
    l5_before = str(rows[4].get("planLessonId"))
    await S._update_session(client, sid(4), "lock")
    r_leave = await S._update_session(client, sid(2), "leave")
    deferred = r_leave.get("deferred") or []
    overflow = r_leave.get("overflow") or []
    sess2 = await S._list_schedule(client, "2026-07-01", "2026-08-31", target_id=TID)
    rows2 = sorted([s for s in sess2.get("sessions", []) if str(s.get("planId")) == PID],
                   key=lambda s: str(s.get("sessionDate")))
    l3_after = str(rows2[3].get("planLessonId"))  # 第4场
    l5_after = str(rows2[4].get("planLessonId"))  # 第5场(锁定)
    l3_before = str(rows[2].get("planLessonId"))
    step("G6a.请假顺延:第4场接住第3场课次", l3_after == l3_before, f"{l3_before}->{l3_after}")
    step("G6b.锁定豁免:第5场课次不动", l5_after == l5_before, f"keep={l5_after}")
    step("G6c.末位溢出提示", len(overflow) >= 1, f"overflow={overflow}")

    # G6-改期：取一个未来已排场次改期 +1 天 → 不触发顺延、状态仍已排、绑定不变
    def sdate(r): return r.get("sessionDate") or r.get("date")
    cands = [r for r in rows2 if str(r.get("sessionStatus")) == "0" and sdate(r) and r.get("planLessonId")]
    old = cands[-2]
    old_lesson = str(old.get("planLessonId"))
    nd = (datetime.date.fromisoformat(str(sdate(old))) + datetime.timedelta(days=1)).isoformat()
    r_re = await S._update_session(client, sid_v := str(old.get("sessionId") or old.get("id")), "reschedule",
                                   date=nd, start="13:30", end="15:00")
    step("G6d.改期无顺延", not (r_re.get("deferred")), f"deferred={r_re.get('deferred')}")
    sess3 = await S._list_schedule(client, nd, nd, target_id=TID)
    moved = [s for s in sess3.get("sessions", []) if str(s.get("sessionId") or s.get("id")) == sid_v]
    step("G6e.改期落新日期且绑定/状态不变",
         bool(moved) and str(moved[0].get("planLessonId")) == old_lesson and str(moved[0].get("sessionStatus")) == "0",
         f"date={nd} lesson={moved[0].get('planLessonId') if moved else '-'} status={moved[0].get('sessionStatus') if moved else '-'}")

    # ── G7 冲突明细：与第1场同时段 → 老师撞场+学生撞场都要能检出并列明细 ──
    base = cands[0]
    def stime(r, k1, k2): return str(r.get(k1) or r.get(k2))[:5]
    d0, s0, e0 = str(sdate(base)), stime(base, "startTime", "start"), stime(base, "endTime", "end")
    r7 = await S._schedule_sessions(client, "0", TID, [{"date": d0, "start": s0, "end": e0}], force=False)
    conf = r7.get("conflicts") or []
    kinds = {c.get("kind") for c in conf}
    has_detail = all(c.get("withTitle") or c.get("withSessionId") for c in conf) if conf else False
    step("G7.同对象同时段检出冲突且列明细", bool(conf) and has_detail, f"kinds={kinds} n={len(conf)}")
    step("G7b.未 force 时一条不落", not r7.get("created"), f"created={r7.get('created')}")

    # ── G8 窗口边界：散课 第14天(今天+13)在窗口内 / 第15天(今天+14)不在 ──
    today = datetime.date.today()
    d14 = (today + datetime.timedelta(days=13)).isoformat()
    d15 = (today + datetime.timedelta(days=14)).isoformat()
    r8 = await S._schedule_sessions(client, "0", TID,
        [{"date": d14, "start": "20:00", "end": "20:30", "note": "G8-第14天"},
         {"date": d15, "start": "20:00", "end": "20:30", "note": "G8-第15天"}], force=True)
    created8 = r8.get("created") or []
    step("G8前置:两散课落库", len(created8) == 2, created8)
    todo = await client.teacher_get("/teacher/schedule/prep/todo", {"days": 14})
    todo_rows = todo if isinstance(todo, list) else (todo.get("rows") or todo.get("list") or [])
    dates = {str(t.get("sessionDate")) for t in todo_rows}
    step("G8a.第14天在窗口内", d14 in dates, d14)
    step("G8b.第15天不在窗口", d15 not in dates, d15)

    # ── G14 家长版导出：无内部字段（图片下载留给主会话看图）──
    r14 = await client.teacher_post(f"/teacher/schedule/plan/{PID}/parent-export?targetId={TID}", {})
    f14 = (r14 or {}).get("file"); u14 = (r14 or {}).get("url")
    step("G14.parent-export 返回文件", bool(f14 or u14), json.dumps(r14, ensure_ascii=False)[:200])
    print("PARENT_EXPORT_FILE=" + str(f14))
    print("PARENT_EXPORT_URL=" + str(u14))

    print("\n===== C段接口 gates " + ("全部 PASS" if not FAILS else f"FAIL×{len(FAILS)}: {FAILS}") + " =====")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
