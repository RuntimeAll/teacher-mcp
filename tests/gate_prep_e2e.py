"""G10（备课链 E2E）：MCP 工具走完整备课链，真库落对象/计划/场次/备课卷（PRD-B-101 卷位模型）。

链路：login → ingest_items(1 题带标记，供组卷引用) → create_teach_target(student)拿 target_id
     → upsert_course_plan(挂该 target,1 lesson，🔴 带 paper_slots 专项卷位)拿 plan_id/lesson_id
     → schedule_sessions(排 1 场) → list_schedule 断言该场在
     → create_paper(question_ids=[标记题], lesson_id+slot_seq=1)建【备课卷】即绑卷位 → 拿 paper_id
     → bind_paper_slot(manual_ready)证工具可调、返回结构含 paper_slots
     → get_plan_detail 断言 plan/lesson 链贯通 + 课次带 paperSlots。
🔴 语义（PRD-B-101 移植）：课次内容模型 = paper_slots『专项卷位』（替代旧 seg_template/备课包）；
   备课态服务端按 paper_slots 推导；组卷带 lesson_id+slot_seq → BE 自动落 paper_kind='2' + 绑卷位；
   target_type 'student'→'0'；id 全链路字符串。
产出对象（target/plan/session/paper）留库（名称带 [PRD-O-005-TEST]，无删除工具属预期），末尾打印 id 供清理。
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
                # 🔴 PRD-B-101：课次专项卷位（替代旧 seg_template），逐卷位组卷绑于此
                "paper_slots": [{
                    "slot_seq": 1,
                    "name": f"{MARK}课内同步卷",
                    "style": "课内同步",
                    "rules": "",
                    "note": "",
                }],
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

        # ── 5. 逐卷位组卷：create_paper(lesson_id+slot_seq) 建【备课卷】即绑卷位 1（PRD-B-101 主路）──
        cp = (await c.call_tool("create_paper", {
            "name": f"{MARK}课内同步卷",
            "question_ids": [int(qid)],
            "lesson_id": lesson_id,
            "slot_seq": 1,
        })).data
        assert cp.get("ok"), f"create_paper(备课卷绑卷位) 失败: {cp}"
        paper_id = str(cp.get("paper_id"))
        assert paper_id and paper_id != "None", f"未拿到 paper_id: {cp}"

        # ── 5b. bind_paper_slot(manual_ready)：证工具可调 + 返回结构含 paper_slots ──
        #   🔴 BE 能力探针：PRD-B-101 的 paper_slots/slot 端点仅在 B 线 BE；C 线 master-ai(:9090)
        #   仅迁了 DB 列、未合并 Java 代码（CoursePlanService/Controller 仍 seg_template）。
        #   命中 404「请求地址不存在」→ BE 未就绪，本 gate 端到端部分优雅 skip（MCP 层已就绪，待 BE merge）。
        mr = (await c.call_tool("bind_paper_slot", {
            "lesson_id": lesson_id,
            "action": "manual_ready",
            "ready": True,
        })).data
        if not mr.get("ok"):
            err = str(mr.get("error", ""))
            if "404" in err or "请求地址不存在" in err or "不存在" in err:
                pytest.skip(
                    "C 线 BE(:9090) 未合并 PRD-B-101 paper_slots/slot 端点（仅 DB 列已迁移）；"
                    "MCP 层移植已就绪，待 BE 从 B 线合并后本 gate 转真端到端。"
                    f"（bind_paper_slot 探针返回: {mr}；已建 target={target_id} plan={plan_id} "
                    f"lesson={lesson_id} session={session_id} paper={paper_id}）")
            assert False, f"bind_paper_slot(manual_ready) 失败(非 BE 缺失): {mr}"
        # 成功路径才校验 MCP 层返回契约（error 路径无这两个键）
        assert "paper_slots" in mr and "prep_state" in mr, (
            f"bind_paper_slot 返回结构缺 paper_slots/prep_state 字段（MCP 层契约）: {mr}")

        # ── 6. get_plan_detail 断言 plan/lesson 链贯通 + 课次带 paperSlots ──
        gd = (await c.call_tool("get_plan_detail", {"plan_id": plan_id})).data
        assert gd.get("ok"), f"get_plan_detail 失败: {gd}"
        plan = gd.get("plan") or {}
        lessons = gd.get("lessons") or []
        assert str(plan.get("id")) == plan_id, f"plan.id 不符: {plan}"
        detail_lessons = {str(x.get("id")): x for x in lessons if isinstance(x, dict)}
        assert lesson_id in detail_lessons, (
            f"课次 {lesson_id} 不在计划明细中: {set(detail_lessons)}")
        my_lesson = detail_lessons[lesson_id]
        slots = my_lesson.get("paperSlots") or my_lesson.get("paper_slots") or []
        assert slots, f"课次 {lesson_id} 未回读到 paperSlots（PRD-B-101 卷位模型）: {my_lesson}"

        print(f"G10 PASS 链: target={target_id} plan={plan_id} lesson={lesson_id} "
              f"session={session_id} paper={paper_id} qid={qid} prep_state={mr.get('prep_state')}")
