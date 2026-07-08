"""MCP 工具·备课组（prep 角色）：
  教学安排与备课闭环 11 工具（create_teach_target/list_teach_targets/upsert_course_plan/
    schedule_sessions/list_schedule/update_session/build_prep_pack/render_prep_pack/
    submit_review/get_student_profile/get_plan_detail）
  组卷 4 工具（compose_paper/create_paper/update_paper/bind_paper_slot）

PRD-O-005 重建：合并旧 app/tools/{schedule,compose}.py；单 client（:9090，A/C 已合并），
删 cluster.ensure_c()，工具直接用注入的 client。schedule 保留「模块级 async 纯函数 + @tool 薄包」结构。
🔴 _standard_scores 在本模块 module 级定义（data_qbank.ingest_items 成卷分值复用它）。
🔴 PRD-B-101（B 线移植）：课次内容模型 seg_template『段模板』→ paper_slots『专项卷位』契约平移；
   新增 bind_paper_slot（卷位绑定管理）；compose_paper/create_paper 加 lesson_id+slot_seq 建卷即绑卷位；
   build_prep_pack/render_prep_pack 退役（保留 @tool 返退役指引，MCP 不再出 PDF，PDF 走平台前端导出）。
"""
from typing import Optional

from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/schedule"


# ═════════════════════ 组卷（平移自 compose.py）═════════════════════
class OutlineItem(BaseModel):
    """组卷大纲一项 = 在某知识点上出 N 道某题型某难度的题。"""

    subjectId: str = Field(description="知识点叶子 id（来自 list_kg_tree，严禁编造）")
    subjectName: str = Field(default="", description="知识点名（可选，仅展示）")
    questionType: int = Field(default=1, description="题型(字典 biz_question_type)：1选择 4填空 5解答 7计算 …")
    difficult: int = Field(default=2, description="难度 1-4")
    count: int = Field(default=5, description="该项题数")


def _standard_scores(types, total):
    """按年级标准分算每题分值（通值）：选择/判断=3 / 填空=3 / 大题类(应用/解答/作图/计算/证明)铺满到 total，余数加在靠后难题。"""
    SMALL, FILL = {1, 2}, {4}   # 1选择/2判断=3分、4填空=3分；3应用/5解答/6作图/7计算/8证明=else 铺满（字典 biz_question_type）
    PER_SMALL = PER_FILL = 3
    scores = [0] * len(types)
    big_idx, used = [], 0
    for i, t in enumerate(types):
        if t in SMALL:
            scores[i] = PER_SMALL
            used += PER_SMALL
        elif t in FILL:
            scores[i] = PER_FILL
            used += PER_FILL
        else:
            big_idx.append(i)
    if big_idx:
        remaining = max(total - used, len(big_idx) * 4)  # 解答至少 4 分/题
        base = remaining // len(big_idx)
        rem = remaining - base * len(big_idx)
        for k, i in enumerate(big_idx):
            scores[i] = base + (1 if k >= len(big_idx) - rem else 0)
    return scores


# ═════════════════════ schedule 小工具：枚举/字段映射 ═════════════════════
def _tt_code(v) -> str:
    """target_type 归一：'student'→'0' / 'class'→'1'；已是 '0'/'1' 原样返回。"""
    if v in ("0", "1"):
        return v
    m = {"student": "0", "class": "1", "学生": "0", "班级": "1"}
    if v not in m:
        raise RuoyiError(f"target_type 非法: {v}（应为 'student' 或 'class'）")
    return m[v]


def _lesson_type_code(v) -> str:
    """lesson_type 归一：'教学'→'0' / '测试'→'1'；已是 '0'/'1' 原样返回。"""
    m = {"0": "0", "1": "1", "教学": "0", "测试": "1", "test": "1", "teach": "0"}
    return m.get(str(v), str(v))


# 🔴 PRD-B-101：seg_template（段模板）→ paper_slots（专项卷位）契约平移。
#    旧字段收到即报错拒绝——不静默兼容，防「两套字段并存」漂移。
def _reject_deprecated_seg(d: dict, old: str, new: str) -> None:
    """收到已退役的 seg_template 系字段 → 立即抛错，明确指引新字段名与结构。"""
    if isinstance(d, dict) and old in d:
        raise RuoyiError(
            f"字段 '{old}' 已退役(PRD-B-101 备课材料×组卷归并)，请改用 '{new}'。"
            f"课次内容模型已从『段模板 seg_template』升维为『专项卷位 paper_slots』：一课次 = N 张专项卷，"
            f"每卷位可绑一张卷。新结构 paper_slots=[{{slot_seq:int, name:str(必填非空), style:str, "
            f"rules:str, note:str}}]；绑卷字段(paper_id/manual_ready)由服务端管，agent 写入通常只给 "
            f"slot_seq/name/style/rules/note。备课=逐卷位 compose_paper/create_paper(lesson_id,slot_seq)+bind，"
            f"见 get_role_manual(role='prep')。"
        )


_LESSON_KEYMAP = {
    "id": "id", "lesson_seq": "lessonSeq", "title": "title", "lesson_type": "lessonType",
    "tag": "tag", "source_ref": "sourceRef", "thinking_action": "thinkingAction",
    "layer_target": "layerTarget", "parent_copy": "parentCopy",
    "kg_node_ids": "kgNodeIds", "paper_slots": "paperSlots",
    # 🔴 PRD-B-101：seg_template → paper_slots；旧字段走 _reject_deprecated_seg 拒收，不再映射
    # R1b S3：prep_state 已删列，备课状态唯一权威 = 服务端按 paper_slots 推导，不再收写入
}


def _map_lesson(d: dict) -> dict:
    _reject_deprecated_seg(d, "seg_template", "paper_slots")
    out: dict = {}
    for k, v in d.items():
        ck = _LESSON_KEYMAP.get(k, k)
        if ck == "lessonType":
            v = _lesson_type_code(v)
        if ck == "id" and v is not None:
            v = str(v)
        out[ck] = v
    return out


def _map_seg(s: dict) -> dict:
    """pack 段：{name,style,question_ids:[str],rules?,note?,groups?} → BE camelCase；question_id 强制 str 防截尾。

    groups（BUG-004 段内分组，可选）= [{title, question_ids:[str]}]，透传给 BE（渲染时组标题起小节）。
    """
    out = {
        "name": s.get("name"),
        "style": s.get("style", ""),
        "questionIds": [str(q) for q in (s.get("question_ids") or s.get("questionIds") or [])],
        "rules": s.get("rules", ""),
        "note": s.get("note", ""),
    }
    groups = s.get("groups")
    if groups:
        out["groups"] = [
            {"title": g.get("title", ""),
             "question_ids": [str(q) for q in (g.get("question_ids") or g.get("questionIds") or [])]}
            for g in groups
        ]
    return out


def _map_item_result(r: dict) -> dict:
    """回收逐题：{question_id?,seg,seq,result,cause} → BE camelCase。result∈对/错/卡, cause∈计算/概念辨析/策略/其他。"""
    out = {
        "seg": r.get("seg", ""),
        "seq": r.get("seq"),
        "result": r.get("result"),
        "cause": r.get("cause", ""),
    }
    qid = r.get("question_id") or r.get("questionId")
    if qid is not None:
        out["questionId"] = str(qid)
    return out


def _map_session_item(it: dict) -> dict:
    """排课一场：{date,start,end,plan_lesson_id?,session_type?,external_title?,note?} → BE camelCase。"""
    out = {"date": it.get("date"), "start": it.get("start"), "end": it.get("end")}
    if it.get("plan_lesson_id") is not None or it.get("planLessonId") is not None:
        out["planLessonId"] = str(it.get("plan_lesson_id") or it.get("planLessonId"))
    st = it.get("session_type") or it.get("sessionType")
    if st is not None:
        out["sessionType"] = str(st)
    if it.get("external_title") or it.get("externalTitle"):
        out["externalTitle"] = it.get("external_title") or it.get("externalTitle")
    if it.get("note"):
        out["note"] = it.get("note")
    return out


def _first_id(resp) -> Optional[str]:
    """从 {id:..} / 裸标量 里取 id 并 str 化。"""
    if isinstance(resp, dict):
        v = resp.get("id")
        return str(v) if v is not None else None
    return str(resp) if resp is not None else None


def _rows(resp) -> list:
    """page/list 响应归一成 list：裸 list / {rows} / {records} / {items} 都吃。"""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("rows", "records", "items", "sessions", "list"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


# ═════════════════════ schedule 核心纯函数（G13 直接 import 调用）═════════════════════
async def _create_teach_target(client, target_type, name, grade_no=None, grade_year=None,
                               textbook_edition="", subject="", parent_phone="",
                               profile=None, color="") -> dict:
    """R1a 建模：grade/textbook 文本 → gradeNo(int)/gradeYear(int)/textbookEdition(码)/subject(码)。"""
    body: dict = {"targetType": _tt_code(target_type), "name": name}
    if grade_no is not None:
        body["gradeNo"] = int(grade_no)
    if grade_year is not None:
        body["gradeYear"] = int(grade_year)
    if textbook_edition:
        body["textbookEdition"] = str(textbook_edition)
    if subject:
        body["subject"] = str(subject)
    if parent_phone:
        body["parentPhone"] = parent_phone
    if color:
        body["color"] = color
    if profile:
        body["profileJson"] = profile
    resp = await client.teacher_post(f"{BASE}/target", body)
    return {"ok": True, "id": _first_id(resp)}


async def _list_teach_targets(client, target_type=None, keyword="", include_archived=False) -> dict:
    params: dict = {"includeArchived": include_archived}
    if target_type:
        params["targetType"] = _tt_code(target_type)
    if keyword:
        params["keyword"] = keyword
    resp = await client.teacher_get(f"{BASE}/target/page", params)
    return {"ok": True, "items": _rows(resp)}


async def _upsert_course_plan(client, plan: dict, lessons=None) -> dict:
    pid = plan.get("id")
    body = {
        "name": plan.get("name"),
        "targetType": _tt_code(plan.get("target_type")),
        "termTag": plan.get("term_tag"),
        "year": plan.get("year"),
    }
    # R1a·S1：计划归属对象（BE 建计划强校验，必传）
    tid = plan.get("target_id") or plan.get("targetId")
    if tid is not None:
        body["targetId"] = str(tid)
    _reject_deprecated_seg(plan, "default_seg_template", "default_paper_slots")
    if plan.get("material_note") is not None:
        body["materialNote"] = plan.get("material_note")
    if plan.get("default_paper_slots") is not None:
        body["defaultPaperSlots"] = plan.get("default_paper_slots")
    if plan.get("status") is not None:
        body["status"] = str(plan.get("status"))
    if pid:
        body["id"] = str(pid)
        resp = await client.teacher_put(f"{BASE}/plan/{pid}", body)
        plan_id = str(pid)
    else:
        resp = await client.teacher_post(f"{BASE}/plan", body)
        plan_id = _first_id(resp)
    lesson_ids: list = []
    if lessons:
        lresp = await client.teacher_post(
            f"{BASE}/plan/{plan_id}/lessons", {"lessons": [_map_lesson(x) for x in lessons]}
        )
        if isinstance(lresp, dict):
            raw = lresp.get("lessonIds") or lresp.get("ids") or lresp.get("rows") or []
        elif isinstance(lresp, list):
            raw = lresp
        else:
            raw = []
        lesson_ids = [str(x.get("id") if isinstance(x, dict) else x) for x in raw]
    return {"ok": True, "plan_id": plan_id, "lesson_ids": lesson_ids}


async def _schedule_sessions(client, target_type, target_id, items, plan_id=None,
                            auto_bind=True, force=False) -> dict:
    body = {
        "targetType": _tt_code(target_type),
        "targetId": str(target_id),
        "autoBind": auto_bind,
        "force": force,
        "items": [_map_session_item(it) for it in items],
    }
    if plan_id:
        body["planId"] = str(plan_id)
    resp = await client.teacher_post(f"{BASE}/session/batch", body)
    created = resp.get("created", []) if isinstance(resp, dict) else []
    conflicts = resp.get("conflicts", []) if isinstance(resp, dict) else []
    return {"ok": True, "created": created, "conflicts": conflicts}


async def _list_schedule(client, start, end, target_id=None) -> dict:
    params = {"start": start, "end": end}
    if target_id:
        params["targetId"] = str(target_id)
    resp = await client.teacher_get(f"{BASE}/session/calendar", params)
    return {"ok": True, "sessions": _rows(resp)}


async def _update_session(client, session_id, action, date=None, start=None, end=None,
                         plan_lesson_id=None, note=None) -> dict:
    sid = str(session_id)
    base = f"{BASE}/session/{sid}"
    if action == "reschedule":
        resp = await client.teacher_put(base, {"date": date, "start": start, "end": end})
    elif action == "rebind":
        resp = await client.teacher_put(base, {"planLessonId": str(plan_lesson_id)})
    elif action == "note":
        resp = await client.teacher_put(base, {"note": note})
    elif action == "leave":
        resp = await client.teacher_post(f"{base}/leave", {})
    elif action == "cancel":
        resp = await client.teacher_post(f"{base}/cancel", {})
    elif action == "mark_done":
        resp = await client.teacher_post(f"{base}/mark-done", {})
    elif action == "lock":
        resp = await client.teacher_post(f"{base}/lock", {})
    elif action == "unlock":
        resp = await client.teacher_post(f"{base}/unlock", {})
    else:
        return {"ok": False, "error": f"未知 action: {action}（应为 reschedule/leave/cancel/mark_done/lock/unlock/rebind/note）"}
    out = {"ok": True}
    if isinstance(resp, dict):
        if resp.get("deferred") is not None:
            out["deferred"] = resp["deferred"]
        if resp.get("overflow") is not None:
            out["overflow"] = resp["overflow"]
    return out


async def _build_prep_pack(client, lesson_id=None, session_id=None, segs=None) -> dict:
    if not lesson_id and not session_id:
        return {"ok": False, "error": "lesson_id 或 session_id 二选一必填"}
    body: dict = {"segs": [_map_seg(s) for s in (segs or [])]}
    if lesson_id:
        body["planLessonId"] = str(lesson_id)
    if session_id:
        body["sessionId"] = str(session_id)
    resp = await client.teacher_post(f"{BASE}/prep-pack", body)
    return {"ok": True, "pack_id": _first_id(resp)}


async def _render_prep_pack(client, pack_id, mark_ready=True) -> dict:
    resp = await client.teacher_post(f"{BASE}/prep-pack/{pack_id}/render", {"markReady": mark_ready})
    arts = resp.get("artifacts", []) if isinstance(resp, dict) else (resp or [])
    return {"ok": True, "artifacts": arts}


async def _submit_review(client, session_id, item_results, teacher_note=None,
                        parent_msg_override=None) -> dict:
    body: dict = {"itemResults": [_map_item_result(r) for r in item_results]}
    if teacher_note:
        body["teacherNote"] = teacher_note
    if parent_msg_override:
        body["parentMsgOverride"] = parent_msg_override
    resp = await client.teacher_post(f"{BASE}/session/{session_id}/review", body)
    if isinstance(resp, dict):
        return {"ok": True, "parent_msg": resp.get("parentMsg"), "portrait_delta": resp.get("portraitDelta")}
    return {"ok": True, "parent_msg": None, "portrait_delta": None}


async def _get_student_profile(client, target_id, target_type="student") -> dict:
    resp = await client.teacher_get(f"{BASE}/target/{target_id}", {})
    profile = None
    if isinstance(resp, dict):
        profile = resp.get("profileJson") or resp.get("profile")
    return {"ok": True, "target": resp, "profile": profile}


async def _get_plan_detail(client, plan_id) -> dict:
    """读某课程计划全部课次明细。GET /teacher/schedule/plan/{id}。plan 与 lessons 拆开返。"""
    resp = await client.teacher_get(f"{BASE}/plan/{plan_id}", {})
    if not isinstance(resp, dict):
        return {"ok": True, "plan": None, "lessons": []}
    lessons = resp.get("lessons") or []
    plan = {k: v for k, v in resp.items() if k not in ("lessons",)}
    return {"ok": True, "plan": plan, "lessons": lessons}


# ═════════════════════ MCP 工具注册 ═════════════════════
def register(mcp, client: RuoyiClient) -> None:
    # ── 组卷 3 工具 ──
    @mcp.tool(tags={"prep"})
    async def compose_paper(outline: list[OutlineItem], title: str = "",
                            lesson_id: str = "", slot_seq: int = 0) -> dict:
        """按大纲从真题库确定性组卷并真落库 biz_paper，归属当前登录 teacher。

        参数:
          outline: [{subjectId, subjectName?, questionType, difficult, count}, ...]
                   subjectId 取自 list_kg_tree 的叶子 id；编排层负责选点，本工具不二次解析意图。
          title:   卷名（可选，默认"MCP组卷"）。
          lesson_id / slot_seq: 🔴 PRD-B-101 备课卷位绑定（可选，二者必须**同现**）——给了则本卷落
                   【备课卷】(paper_kind='2') 并绑到该课次卷位；只给一个 → 本地报错不发请求；
                   都省 = 普通卷（一切照旧）。🔴 备课卷私有，绝不 set-public。
        返回:
          {ok, paper_id, item_count, paper, notes}；底座不在/无匹配题 → {ok:false, reason}（不假成功）。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        has_lesson, has_slot = bool(lesson_id), bool(slot_seq and int(slot_seq) > 0)
        if has_lesson != has_slot:
            return {"ok": False, "reason": "lesson_id 与 slot_seq 必须同现（备课卷位绑定）："
                    "只给一个无效。要么都给（绑卷位=备课卷 paper_kind=2），要么都省（普通卷）。"}
        teacher_id: Optional[int] = client.user_id
        if teacher_id is None:
            return {"ok": False, "reason": "会话无 teacher_id，请重新 login"}
        cleaned = [it.model_dump() for it in outline if it.subjectId and it.count > 0]
        if not cleaned:
            return {"ok": False, "reason": "outline 为空或无有效项（需 subjectId + count>0）"}

        body = {
            "title": title or "MCP组卷",
            "outline": cleaned,
            "dedup": True,
            "save": True,          # 🔴 真落库
            "teacherId": teacher_id,  # 🔴 归属当前登录 teacher
        }
        if has_lesson:            # 🔴 PRD-B-101：透传绑卷位（BE 自动 paper_kind='2' + 绑 slot）
            body["lessonId"] = str(lesson_id)
            body["slotSeq"] = int(slot_seq)
        try:
            resp = await client.auto_generate(body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"组卷失败: {e}"}
        if not isinstance(resp, dict):
            resp = {}
        paper = resp.get("paper") or {}
        paper_id = resp.get("paperId") or paper.get("id")
        if not paper_id:
            return {"ok": False, "reason": "组卷接口未返回 paperId（可能题库无匹配题）", "raw": resp}

        # notes：后端回传可能是 str，包成 list 防按字符拆（C-001 坑）
        raw_notes = resp.get("notes")
        if isinstance(raw_notes, str):
            notes = [raw_notes] if raw_notes.strip() else []
        elif isinstance(raw_notes, list):
            notes = [str(n) for n in raw_notes if str(n).strip()]
        else:
            notes = []

        # 题目嵌在 paper.sections[].questions 下；兜底取 totalCount/question_count
        item_count = 0
        sections = paper.get("sections")
        if isinstance(sections, list):
            for sec in sections:
                qs = sec.get("questions") if isinstance(sec, dict) else None
                if isinstance(qs, list):
                    item_count += len(qs)
        if item_count == 0:
            item_count = paper.get("totalCount") or paper.get("questionCount") or 0
        return {
            "ok": True,
            "paper_id": paper_id,
            "item_count": item_count,
            "paper": paper,
            "notes": notes,
        }

    @mcp.tool(tags={"prep"})
    async def create_paper(name: str, question_ids: list[int], paper_category_id: str = "",
                           lesson_id: str = "", slot_seq: int = 0) -> dict:
        """按指定题目 id 列表（顺序即试卷内题号顺序）组装成一套**试卷**入卷库，归属当前登录 teacher。

        用于整卷录入：题目已 ingest_question 入库后，把它们按原卷题号顺序串成 biz_paper（建 section + biz_paper_question 关联）。
        参数:
          name: 试卷名（如原卷标题），1-200 字符。
          question_ids: 题目 id 列表，**顺序 = 试卷内题号顺序**，至少 1 题。
          paper_category_id: 试卷分类 id（可选，卷库目录树；空=根级）。
          lesson_id / slot_seq: 🔴 PRD-B-101 备课卷位绑定（可选，二者必须**同现**）——给了则本卷落
                   【备课卷】(paper_kind='2') 并绑到该课次卷位；只给一个 → 本地报错不发请求；
                   都省 = 普通卷。🔴 备课卷私有，绝不 set-public。
        返回: {ok, paper_id, ...}；异常 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not name or not question_ids:
            return {"ok": False, "reason": "name 与 question_ids 必填"}
        has_lesson, has_slot = bool(lesson_id), bool(slot_seq and int(slot_seq) > 0)
        if has_lesson != has_slot:
            return {"ok": False, "reason": "lesson_id 与 slot_seq 必须同现（备课卷位绑定）："
                    "只给一个无效。要么都给（绑卷位=备课卷 paper_kind=2），要么都省（普通卷）。"}
        body = {"name": name, "questionIds": [int(q) for q in question_ids]}
        if paper_category_id:
            body["paperCategoryId"] = paper_category_id
        if has_lesson:            # 🔴 PRD-B-101：透传绑卷位（BE 自动 paper_kind='2' + 绑 slot，事务一致）
            body["lessonId"] = str(lesson_id)
            body["slotSeq"] = int(slot_seq)
        try:
            resp = await client.teacher_post("/teacher/exam/paper/create", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"建卷失败: {e}"}
        resp = resp or {}
        pid = resp.get("paperId") or resp.get("id") or (resp.get("paper") or {}).get("id")
        if not pid:
            return {"ok": False, "reason": "建卷接口未返回 paperId", "raw": resp}
        return {"ok": True, "paper_id": pid, "name": name, "question_count": len(question_ids)}

    @mcp.tool(tags={"prep"})
    async def update_paper(paper_id: int, total_score: int = 120, suggest_time: int = 120) -> dict:
        """给试卷按**年级标准分(通值)**算每题分值 + 设建议时长，走 /update 落库。

        分值规则（不抠原卷，按常规给）：选择/判断=3分、填空=3分，大题类(应用/解答/作图/计算/证明)把剩余分铺满到 total_score（余数加在靠后的难题）。
        参数: paper_id；total_score 总分(初中数学期末常规 120)；suggest_time 建议时长(分钟,常规 120)。
        返回: {ok, paper_id, total, per_question:[...]}；异常 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            detail = await client.teacher_post("/teacher/exam/paper/detail", {"paperId": int(paper_id)})
        except RuoyiError as e:
            return {"ok": False, "reason": f"取详情失败: {e}"}
        secs = (detail or {}).get("sections") or []
        # 收集 (sectionId, questionId, questionType) 按 section/题序
        flat = []
        for sec in secs:
            sid = sec.get("sectionId")
            for q in sec.get("questions") or []:
                flat.append((sid, q.get("id"), int(q.get("questionType") or 1)))
        if not flat:
            return {"ok": False, "reason": "试卷无题目"}

        types = [t for _, _, t in flat]
        scores = _standard_scores(types, total_score)
        # 同 section 内 sort 递增（uk_section_sort）
        sec_seq = {}
        questions = []
        for (sid, qid, _t), sc in zip(flat, scores):
            sec_seq[sid] = sec_seq.get(sid, 0) + 1
            questions.append({"questionId": int(qid), "sectionId": sid, "sort": sec_seq[sid], "score": sc})
        body = {"paperId": int(paper_id), "questions": questions, "suggestTime": suggest_time}
        try:
            await client.teacher_post("/teacher/exam/paper/update", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"更新失败: {e}"}
        return {"ok": True, "paper_id": paper_id, "total": sum(scores), "per_question": scores}

    @mcp.tool(tags={"prep"})
    async def bind_paper_slot(lesson_id: str, slot_seq: int = 0, action: str = "bind",
                              paper_id: str = "", ready: bool = True) -> dict:
        """🔴 PRD-B-101 备课卷位绑定管理（绑既有卷 / 解绑 / 标记已备好）→ 备课线。返回 {ok, paper_slots, prep_state}。

        备课主路 = 组卷时直接带 lesson_id+slot_seq 建卷即自动绑（create_paper/compose_paper）；
        本工具是**事后管理**：把已有卷挂到卷位（D7 兜底）、解绑、或手动标记整课次已备好。
        🔴 备课卷私有，本工具不含任何公开化能力（绝不 set-public）。
        参数:
          lesson_id : 课次 id（字符串雪花号）——必填。
          slot_seq  : 卷位序号（≥1）——action=bind/unbind 必填；action=manual_ready 忽略（课次级）。
          action    : 动作枚举——
            'bind'         绑既有卷到卷位（传 paper_id，必须真实存在且归我；BE 自动置该卷 paper_kind='2'）
            'unbind'      解绑卷位（卷留库不删；🔴 解绑会自动清该课次 manual_ready=false）
            'manual_ready' 手动标记整课次备课态（传 ready；0 卷位课次 → BE 400）
          paper_id  : action=bind 时必填（要挂的既有卷 id，字符串雪花号）。
          ready     : action=manual_ready 时的目标态（True=已备好，默认 True）。
        返回: {ok, paper_slots:[...], prep_state:'0未备/1备课中/2已备好'}；异常 → {ok:false, error}。
        """
        if not client.has_session():
            return {"ok": False, "error": "需先 login"}
        if not lesson_id:
            return {"ok": False, "error": "lesson_id 必填"}
        lid = str(lesson_id)
        base = f"{BASE}/plan/lesson/{lid}"
        try:
            if action == "bind":
                if not paper_id:
                    return {"ok": False, "error": "action='bind' 需传 paper_id（要挂的既有卷 id）"}
                if not (slot_seq and int(slot_seq) > 0):
                    return {"ok": False, "error": "action='bind' 需传 slot_seq（≥1）"}
                resp = await client.teacher_post(
                    f"{base}/slot/{int(slot_seq)}/bind", {"paperId": str(paper_id)})
            elif action == "unbind":
                if not (slot_seq and int(slot_seq) > 0):
                    return {"ok": False, "error": "action='unbind' 需传 slot_seq（≥1）"}
                resp = await client.teacher_post(f"{base}/slot/{int(slot_seq)}/unbind", {})
            elif action == "manual_ready":
                resp = await client.teacher_post(f"{base}/manual-ready", {"ready": bool(ready)})
            else:
                return {"ok": False, "error": f"未知 action: {action}（应为 bind/unbind/manual_ready）"}
        except RuoyiError as e:
            return {"ok": False, "error": f"{action} 失败: {e}"}
        resp = resp or {}
        return {
            "ok": True,
            "paper_slots": resp.get("paperSlots") if isinstance(resp, dict) else None,
            "prep_state": resp.get("prepState") if isinstance(resp, dict) else None,
        }

    # ── 教学安排与备课闭环 11 工具 ──
    @mcp.tool(tags={"prep"})
    async def create_teach_target(
        target_type: str, name: str, grade_no: int = None, grade_year: int = None,
        textbook_edition: str = "", subject: str = "", parent_phone: str = "",
        profile: dict = None, color: str = "",
    ) -> dict:
        """建教学对象档案（学生或班级）→ :9090。返回 {ok, id}（id 为字符串雪花号）。

        对象即「教谁」：一个学生或一个班课，后续排课/备课/回收全挂在它身上。
        🔴 R1a 建模口径：年级/教材不再传文本，改传 gradeNo+gradeYear+字典码。
           暑期录「升四」= grade_no:4, grade_year:2026（gradeYear=该年级生效学年的起始年，
           当前年级由服务端按 9/1 学年进位推导）。
        参数:
          target_type      : 'student'（学生一对一）| 'class'（班课）——必填
          name             : 对象名（学生姓名 / 班级名）
          grade_no         : 年级 1-12（字典 biz_edu_grade；1-6 小学 / 7-9 初中 / 10-12 高中）
          grade_year       : grade_no 生效学年起始年（如 2026 = 2026-09-01 起学年）
          textbook_edition : 教材版本字典码（biz_edu_edition：'1'浙教/'2'人教/'3'北师大/'4'苏教）
          subject          : 学科字典码（biz_edu_subject：'1'数学/'2'科学/'3'语文/'4'英语）
                             （edition/subject 服务端兼容中文标签归一化，但请按码传）
          parent_phone     : 家长手机号
          profile          : 肖像 dict（学生画像）——UI 四格 = traits/level.desc/level.target_layer/error_signals，
                             结构见契约 profile_json：{traits:[str], level:{desc,target_layer}, env:str,
                             history:[{topic,status:'吃透|讲过未吃透',src}],
                             error_signals:[{tag,evidence,session_id,ts,by:'system|teacher',status:'pending|confirmed'}]}
          color            : 色板色（空则服务端从色板轮转分配）
        """
        try:
            return await _create_teach_target(client, target_type, name, grade_no, grade_year,
                                              textbook_edition, subject, parent_phone, profile, color)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def list_teach_targets(
        target_type: str = None, keyword: str = "", include_archived: bool = False,
    ) -> dict:
        """查我名下的教学对象卡片墙（含实时聚合：排课数/绑定计划进度/下一课/班课学员数）→ :9090。

        建对象前查重、选排课对象都走它。返回 {ok, items}。
        参数:
          target_type     : 'student' | 'class'，省略=两类都查
          keyword         : 名称模糊过滤
          include_archived: True 才含已归档对象（默认只看在用的）
        """
        try:
            return await _list_teach_targets(client, target_type, keyword, include_archived)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def upsert_course_plan(plan: dict, lessons: list = None) -> dict:
        """建/改课程计划 + 批量 upsert 课次（一步到位）→ :9090。返回 {ok, plan_id, lesson_ids, view_url}。

        计划 = 一段周期（如一个暑假）的课次编排蓝本；排课时按 lesson_seq 顺序自动绑到场次上。
        🔴 R1a·S1：计划有归属——新建必传 target_type + target_id（BE 强校验对象存在且归我，缺传 400）。
        🔴 PRD-B-101 契约平移：课次内容模型 = **专项卷位 paper_slots**（替代旧 seg_template『段模板』）。
           一课次 = N 张专项卷，每卷位可绑一张卷。**旧字段 seg_template / default_seg_template 收到即报错拒绝**
           （不静默兼容，防两套字段并存漂移）。
        参数:
          plan : {id?, name, target_type:'student|class', target_id:str（归属对象 id，🔴 新建必传）,
                  term_tag:'暑假|上学期|寒假|下学期', year:int,
                  material_note?:str（素材说明，如「学而思 36 周书·挑题制」）,
                  default_paper_slots?:list（默认专项卷位模板，lesson 空则继承）,
                  status?:'0草稿|1启用|2归档'}
                 —— 带 id = 改计划基本维，空 id = 新建。🔴 无 total_lessons（=课次数实时聚合）。
          lessons : 课次列表，每个 dict:{id?（空=新增）, lesson_seq:int, title, lesson_type:'0教学|1测试',
                    tag?（自由标签，吃透课走这）, source_ref?（素材源，如「学而思第10+11周」）,
                    thinking_action?（思维动作）, layer_target?（层数目标，如 '2→3'）,
                    parent_copy?（家长版口语文案）, kg_node_ids?:[str]（课内同步锚的 biz_subject id）,
                    paper_slots?:list（本课次专项卷位模板，覆盖计划默认）：
                    [{slot_seq:int, name:str(必填非空), style:str, rules:str, note:str}]
                    （🔴 绑定字段 paper_id/manual_ready 由服务端管，agent 写入通常只给 slot_seq/name/style/rules/note）}
        """
        try:
            out = await _upsert_course_plan(client, plan, lessons)
            if isinstance(out, dict) and out.get("ok"):
                out["view_url"] = "http://localhost:9091/desk/prep"  # 🔴 备课台深链
            return out
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def schedule_sessions(
        target_type: str, target_id: str, items: list, plan_id: str = None,
        auto_bind: bool = True, force: bool = False,
    ) -> dict:
        """批量排课（给某对象铺一串场次）→ :9090。返回 {ok, created, conflicts}。

        auto_bind=True 时按 lesson_seq 顺序把未排课次自动绑到这批场次上（items 里也可显式 plan_lesson_id）。
        🔴 冲突处理（契约 D6）：命中冲突且 force=False → **一条不落**、只回 conflicts 明细；
           前端弹警告后可 force=True 强存（重发同一批）。冲突口径：老师撞场(create_by 同人时间重叠)/学生撞场。
        参数:
          target_type : 'student' | 'class'
          target_id   : 对象 id（字符串）
          items       : [{date:'YYYY-MM-DD', start:'HH:MM', end:'HH:MM', plan_lesson_id?:str,
                          session_type?:'1正课|2测试|3外部占位', external_title?:str（外部占位标题）, note?:str}]
          plan_id     : 绑定的计划 id（auto_bind 用它取课次顺序）
          auto_bind   : 按 lesson_seq 顺序自动绑未排课次（默认 True）
          force        : True = 无视冲突强存（默认 False，先探冲突）
        """
        try:
            return await _schedule_sessions(client, target_type, target_id, items, plan_id, auto_bind, force)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def list_schedule(start: str, end: str, target_id: str = None) -> dict:
        """查某时间窗的月历场次（对象名/色、时间、课次标题、类型、备课态）→ :9090。返回 {ok, sessions}。

        参数:
          start     : 起始日期 'YYYY-MM-DD'（含）
          end       : 结束日期 'YYYY-MM-DD'（含）
          target_id : 只看某对象的场次（省略=我名下全部）
        """
        try:
            return await _list_schedule(client, start, end, target_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def update_session(
        session_id: str, action: str, date: str = None, start: str = None, end: str = None,
        plan_lesson_id: str = None, note: str = None,
    ) -> dict:
        """改单场次（改期/请假/取消/标已上/锁内容/改绑课次/改备注）→ :9090。返回 {ok, deferred?, overflow?}。

        参数:
          session_id : 场次 id（字符串）
          action     : 动作枚举——
            'reschedule' 改期（传 date/start/end；🔴 改期=只改时间不改状态、不触发顺延）
            'leave'      请假 → 🔴 触发顺延：该对象该计划、日期在其后的「已排」场次，绑定课次整体前移补位；
                         lesson_locked='1' 的场次保持原课次被跳过；末位课次悬空 → overflow 提示需补排
            'cancel'     取消 → 同样触发顺延（口径同 leave）
            'mark_done'  标记已上（session_status→已上）
            'lock'       锁定本场绑定的课次内容（顺延时被跳过、不改绑）
            'unlock'     解锁
            'rebind'     改绑课次（传 plan_lesson_id；只改本场，不动别的场次）
            'note'       改备注（传 note）
          date/start/end : reschedule 用；plan_lesson_id : rebind 用；note : note 用
        返回：leave/cancel 会带 deferred（顺延明细 [{sessionId,newLessonId}]）+ overflow（悬空课次提示）。
        """
        try:
            return await _update_session(client, session_id, action, date, start, end, plan_lesson_id, note)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def build_prep_pack(lesson_id: str = None, session_id: str = None, segs: list = None) -> dict:
        """🔴 DEPRECATED（PRD-B-101 已退役）：备课不再走「装备课包」，改为**按卷位组卷**。

        备课材料模型已从「一包 N 段」升维为「一课次 N 张专项卷位」：逐卷位
        `compose_paper/create_paper(lesson_id, slot_seq)` 建卷（自动落【备课卷】+ 绑卷位）+
        `bind_paper_slot` 管理绑定，PDF 由平台前端导出（MCP 不再出 PDF）。
        本工具仅保留返回退役指引，不再执行任何操作。见 get_role_manual(role='prep')。
        """
        return {
            "ok": False,
            "error": "build_prep_pack 已退役(PRD-B-101)，备课=按卷位 compose_paper+bind，见 get_role_manual(role='prep')",
        }

    @mcp.tool(tags={"prep"})
    async def render_prep_pack(pack_id: str = None, mark_ready: bool = True) -> dict:
        """🔴 DEPRECATED（PRD-B-101 已退役）：MCP 不再出 PDF。

        备课卷 PDF 一律走组卷前端链路（平台「我的卷库·备课卷」导出），服务端简化渲染（纯 Java PDF）退役。
        备课改为逐卷位 `compose_paper/create_paper(lesson_id, slot_seq)` + `bind_paper_slot`。
        本工具仅保留返回退役指引，不再执行任何操作。见 get_role_manual(role='prep')。
        """
        return {
            "ok": False,
            "error": "render_prep_pack 已退役(PRD-B-101)，备课=按卷位 compose_paper+bind，见 get_role_manual(role='prep')",
        }

    @mcp.tool(tags={"prep"})
    async def submit_review(
        session_id: str, item_results: list, teacher_note: str = None, parent_msg_override: str = None,
    ) -> dict:
        """课后回收（录逐题对错）→ 生成家长反馈 + 肖像增量 → :9090。返回 {ok, parent_msg, portrait_delta}。

        提交即标该场次「已上」。parent_msg 服务端模板拼装「家长您好！…思维题：/同步：/拓展奥数：」——
        🔴 R1b S5：parent_msg 即时生成不落库（提交/查详情时都按当时上下文现算，override 也过内部词防线）；
        🔴 内部词（层/★/素材/挑题/薄弱）一律不进家长文案。portrait_delta = 错/卡题按 cause 聚合出的
        error_signals（by=system,status=pending，带 session_id 溯源），自动 append 进对象肖像。重复提交=覆盖+上一版进 prev_json。
        参数:
          session_id         : 场次 id
          item_results       : 逐题结果 [{question_id?:str, seg:'段名', seq:int, result:'对|错|卡',
                               cause:'计算|概念辨析|策略|其他'}]
          teacher_note       : 老师备注（可选）
          parent_msg_override: 传入则用它替换模板文案（LLM 润色位，可选）
        """
        try:
            return await _submit_review(client, session_id, item_results, teacher_note, parent_msg_override)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def get_student_profile(target_id: str, target_type: str = "student") -> dict:
        """取对象详情 + 肖像（含 error_signals 易错库）→ :9090。返回 {ok, target, profile}。

        备课前读画像、看回收后新增的 pending 易错信号都走它。
        参数:
          target_id   : 对象 id（字符串）
          target_type : 'student'（默认）| 'class'
        """
        try:
            return await _get_student_profile(client, target_id, target_type)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def get_plan_detail(plan_id: str) -> dict:
        """读某课程计划的全部课次明细（🔴 备课前读「这节课的编排蓝本」）→ :9090
        GET /teacher/schedule/plan/{id}。返回 {ok, plan, lessons}。

        当前链条最硬的读缺口：现有 upsert_course_plan 只写、list_schedule 只给场次概览，读不到
        某课次的分段蓝本 / 课内锚点 → 圈题无依据。本工具补上。
        参数:
          plan_id : 课程计划 id（字符串雪花号；list_schedule 的场次里带 plan_lesson_id 可回溯到 plan）。
        返回:
          plan    : {id, name, targetType, targetId, termTag, year, materialNote,
                     defaultPaperSlots（计划默认专项卷位模板）, status, createTime, updateTime, lessonCount}
          lessons : [{id, planId, lessonSeq, title, lessonType('0'教学/'1'测试), tag, sourceRef,
                     thinkingAction, layerTarget（层数目标如 '2→3'）, parentCopy（家长版文案，🔴 家长可见、
                     无内部词）, kgNodeIds:[str]（🔴 课内锚点，直接喂 search_questions(subject_id=)）,
                     paperSlots:[{slot_seq,name,style,rules,note,paper_id,manual_ready}]（🔴 PRD-B-101 专项卷位蓝本：
                     每卷位对应一张专项卷；空卷位=待组，逐卷位走 compose_paper/create_paper(lesson_id,slot_seq)）,
                     paperSlotsInherited（true=继承自计划 default_paper_slots）,
                     prepState（备课态，服务端按 paper_slots 推导：'0'未备/'1'备课中/'2'已备好）}]
        """
        try:
            return await _get_plan_detail(client, plan_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
