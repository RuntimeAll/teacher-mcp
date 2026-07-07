"""G10（备课链 E2E）：MCP 工具走完整备课链，真库落对象/计划/场次/备课包。

链路：login → ingest_items(1 题带标记，供备课包引用) → create_teach_target(student)拿 target_id
     → upsert_course_plan(挂该 target,1 lesson)拿 plan_id/lesson_id → schedule_sessions(排 1 场)
     → list_schedule 断言该场在 → build_prep_pack(段引用标记题 qid)拿 pack_id
     → get_plan_detail 断言 plan/lesson 链贯通。
🔴 语义（记忆沉淀）：pack.status = 备课状态唯一权威；target_type 'student'→'0'；id 全链路字符串。
产出对象（target/plan/session/pack）留库（名称带 [PRD-O-005-TEST]，无删除工具属预期），末尾打印 id 供清理。
"""
import time

import pytest
from fastmcp import Client

from teacher_mcp.server import build_server

MARK = "[PRD-O-005-TEST]"

# 唯一时段（gates 每跑真落库；同一 admin 老师同段会撞场）——按时间戳散在当天 08:00-17:xx
_slot = int(time.time()) % 600
_START = f"{8 + _slot // 60:02d}:{_slot % 60:02d}"
_END = f"{8 + _slot // 60:02d}:{(_slot % 60 + 1) % 60:02d}" if _slot % 60 < 59 else f"{9 + _slot // 60:02d}:00"
SESSION_DATE = "2026-07-20"


@pytest.mark.asyncio
async def test_prep_chain_e2e():
    async with Client(build_server("all"), timeout=180) as c:
        r = (await c.call_tool("login", {})).data
        assert r.get("ok", True), f"login 失败: {r}"

        # ── 0. 录 1 题带标记（供备课包段引用）──
        ing = (await c.call_tool("ingest_items", {
            "items": [{
                "stem": f"{MARK} 备课链引用题：计算 $5-8$ 的结果。",
                "answer": "$-3$",
                "analyze": f"$5-8=-3$。{MARK}",
                "question_type": 7,
            }],
            "subject_root": "100",
        })).data
        assert ing.get("ok"), f"ingest_items 失败: {ing}"
        qid = str((ing.get("results") or [{}])[0].get("question_id"))
        assert qid and qid != "None", f"未拿到引用题 qid: {ing}"

        # ── 1. 建教学对象（student）──
        t = (await c.call_tool("create_teach_target", {
            "target_type": "student",
            "name": f"{MARK}备课冒烟学生",
            "grade_no": 7,
            "grade_year": 2026,
            "subject": "1",
        })).data
        assert t.get("ok"), f"create_teach_target 失败: {t}"
        target_id = str(t.get("id"))
        assert target_id and target_id != "None", f"未拿到 target_id: {t}"

        # ── 2. 建课程计划 + 1 课次 ──
        p = (await c.call_tool("upsert_course_plan", {
            "plan": {
                "name": f"{MARK}备课冒烟计划",
                "target_type": "student",
                "target_id": target_id,
                "term_tag": "暑假",
                "year": 2026,
            },
            "lessons": [{
                "lesson_seq": 1,
                "title": f"{MARK}第1课·有理数运算",
                "lesson_type": "0",
            }],
        })).data
        assert p.get("ok"), f"upsert_course_plan 失败: {p}"
        plan_id = str(p.get("plan_id"))
        lesson_ids = p.get("lesson_ids") or []
        assert plan_id and plan_id != "None", f"未拿到 plan_id: {p}"
        assert lesson_ids, f"未拿到 lesson_ids: {p}"
        lesson_id = str(lesson_ids[0])

        # ── 3. 排 1 场（近期日期，绑该课次）──
        s = (await c.call_tool("schedule_sessions", {
            "target_type": "student",
            "target_id": target_id,
            "plan_id": plan_id,
            "force": True,   # 🔴 gates 每跑真落库，同 admin 老师易撞场；强存（测试数据带标记可清理）
            "items": [{
                "date": SESSION_DATE,
                "start": _START,
                "end": _END,
                "plan_lesson_id": lesson_id,
            }],
        })).data
        assert s.get("ok"), f"schedule_sessions 失败: {s}"
        created = s.get("created") or []
        assert created, f"未排出场次（conflicts={s.get('conflicts')}）: {s}"
        session_id = None
        for it in created:
            if isinstance(it, dict):
                session_id = str(it.get("id") or it.get("sessionId") or it.get("session_id") or "")
                if session_id and session_id != "None":
                    break
        assert session_id, f"created 无 session id: {created}"

        # ── 4. list_schedule 断言该场在 ──
        ls = (await c.call_tool("list_schedule", {
            "start": "2026-07-01", "end": "2026-07-31", "target_id": target_id,
        })).data
        assert ls.get("ok"), f"list_schedule 失败: {ls}"
        sessions = ls.get("sessions") or []
        sess_ids = {str(x.get("id") or x.get("sessionId")) for x in sessions if isinstance(x, dict)}
        assert session_id in sess_ids, (
            f"排出的场次 {session_id} 不在月历中: {sess_ids} | 原始 {sessions}")

        # ── 5. 装配备课包（段引用标记题 qid）──
        bp = (await c.call_tool("build_prep_pack", {
            "lesson_id": lesson_id,
            "segs": [{
                "name": f"{MARK}同步段",
                "style": "课内同步",
                "question_ids": [qid],
            }],
        })).data
        assert bp.get("ok"), f"build_prep_pack 失败: {bp}"
        pack_id = str(bp.get("pack_id"))
        assert pack_id and pack_id != "None", f"未拿到 pack_id: {bp}"

        # ── 6. get_plan_detail 断言 plan/lesson 链贯通 ──
        gd = (await c.call_tool("get_plan_detail", {"plan_id": plan_id})).data
        assert gd.get("ok"), f"get_plan_detail 失败: {gd}"
        plan = gd.get("plan") or {}
        lessons = gd.get("lessons") or []
        assert str(plan.get("id")) == plan_id, f"plan.id 不符: {plan}"
        detail_lesson_ids = {str(x.get("id")) for x in lessons if isinstance(x, dict)}
        assert lesson_id in detail_lesson_ids, (
            f"课次 {lesson_id} 不在计划明细中: {detail_lesson_ids}")

        print(f"G10 PASS 链: target={target_id} plan={plan_id} lesson={lesson_id} "
              f"session={session_id} pack={pack_id} qid={qid}")
