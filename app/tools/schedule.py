"""MCP 工具·教学安排与备课闭环（PRD-C-213，10 工具）。

🔴 全部打 **C 线 :8090**（挂 /teacher/schedule/**，misikt envelope）——register 收 cluster，
   每个 @mcp.tool() 先 `client = await cluster.ensure_c()`（懒登录 C 线），绝不用 cluster.a（那是 A 线 :8080）。
   现有 14 工具全打 A 线，本模块照 lecture.py 的 C 线范式抄，别照它们传 cluster.a。

🔴 模块结构（G13 驱动脚本要绕开 MCP 协议直接 import 调用）：
   核心逻辑 = 模块级 async 纯函数 `_xxx(client, ...)`（收一个「已 ensure_c 的 C 线 client」）；
   register 里的 @mcp.tool() 只薄包一层（ensure_c + try/except RuoyiError → {ok:False}）。

🔴 id 全链路字符串（雪花 19 位，JSON number 会截尾）——入参 target_id/session_id/pack_id/plan_lesson_id/
   question_id 一律按字符串传，返回的 id 也 str() 化。

映射约定：
  target_type 枚举 'student'|'class' → char(1) '0'|'1'（契约§一）；
  纯函数把 snake_case 入参映射成 BE 的 camelCase（lessonSeq/planLessonId/...）。
"""
from typing import Optional

from app.ruoyi import RuoyiCluster, RuoyiError

BASE = "/teacher/schedule"


# ───────────────────────── 小工具：枚举/字段映射 ─────────────────────────
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


_LESSON_KEYMAP = {
    "id": "id", "lesson_seq": "lessonSeq", "title": "title", "lesson_type": "lessonType",
    "tag": "tag", "source_ref": "sourceRef", "thinking_action": "thinkingAction",
    "layer_target": "layerTarget", "parent_copy": "parentCopy",
    "kg_node_ids": "kgNodeIds", "seg_template": "segTemplate", "prep_state": "prepState",
}


def _map_lesson(d: dict) -> dict:
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
    """pack 段：{name,style,question_ids:[str],rules?,note?} → BE camelCase；question_id 强制 str 防截尾。"""
    return {
        "name": s.get("name"),
        "style": s.get("style", ""),
        "questionIds": [str(q) for q in (s.get("question_ids") or s.get("questionIds") or [])],
        "rules": s.get("rules", ""),
        "note": s.get("note", ""),
    }


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


# ───────────────────────── 核心纯函数（G13 直接 import 调用）─────────────────────────
async def _create_teach_target(client, target_type, name, grade="", subject="",
                               textbook="", parent_phone="", profile=None, color="") -> dict:
    body: dict = {"targetType": _tt_code(target_type), "name": name}
    if grade:
        body["grade"] = grade
    if subject:
        body["subject"] = subject
    if textbook:
        body["textbook"] = textbook
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
    if plan.get("material_note") is not None:
        body["materialNote"] = plan.get("material_note")
    if plan.get("default_seg_template") is not None:
        body["defaultSegTemplate"] = plan.get("default_seg_template")
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


# ───────────────────────── MCP 工具注册（薄包一层）─────────────────────────
def register(mcp, cluster: RuoyiCluster) -> None:
    @mcp.tool()
    async def create_teach_target(
        target_type: str, name: str, grade: str = "", subject: str = "",
        textbook: str = "", parent_phone: str = "", profile: dict = None, color: str = "",
    ) -> dict:
        """建教学对象档案（学生或班级）→ 打 C 线 :8090。返回 {ok, id}（id 为字符串雪花号）。

        对象即「教谁」：一个学生或一个班课，后续排课/备课/回收全挂在它身上。
        参数:
          target_type : 'student'（学生一对一）| 'class'（班课）——必填
          name        : 对象名（学生姓名 / 班级名）
          grade       : 年级（如 '升四' '四年级'）
          subject     : 学科（如 '数学'）
          textbook    : 教材（如 '人教版三年级下册'）
          parent_phone: 家长手机号
          profile     : 肖像 dict（学生画像）——UI 四格 = traits/level.desc/level.target_layer/error_signals，
                        结构见契约 profile_json：{traits:[str], level:{desc,target_layer}, env:str,
                        history:[{topic,status:'吃透|讲过未吃透',src}],
                        error_signals:[{tag,evidence,session_id,ts,by:'system|teacher',status:'pending|confirmed'}]}
          color       : 色板色（空则服务端从色板轮转分配）
        """
        try:
            client = await cluster.ensure_c()
            return await _create_teach_target(client, target_type, name, grade, subject,
                                              textbook, parent_phone, profile, color)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def list_teach_targets(
        target_type: str = None, keyword: str = "", include_archived: bool = False,
    ) -> dict:
        """查我名下的教学对象卡片墙（含实时聚合：排课数/绑定计划进度/下一课/班课学员数）→ C 线 :8090。

        建对象前查重、选排课对象都走它。返回 {ok, items}。
        参数:
          target_type     : 'student' | 'class'，省略=两类都查
          keyword         : 名称模糊过滤
          include_archived: True 才含已归档对象（默认只看在用的）
        """
        try:
            client = await cluster.ensure_c()
            return await _list_teach_targets(client, target_type, keyword, include_archived)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def upsert_course_plan(plan: dict, lessons: list = None) -> dict:
        """建/改课程计划 + 批量 upsert 课次（一步到位）→ C 线 :8090。返回 {ok, plan_id, lesson_ids}。

        计划 = 一段周期（如一个暑假）的课次编排蓝本；排课时按 lesson_seq 顺序自动绑到场次上。
        参数:
          plan : {id?, name, target_type:'student|class', term_tag:'暑假|上学期|寒假|下学期', year:int,
                  material_note?:str（素材说明，如「学而思 36 周书·挑题制」）,
                  default_seg_template?:list（段模板，lesson 空则继承，见契约 seg_template）,
                  status?:'0草稿|1启用|2归档'}
                 —— 带 id = 改计划基本维，空 id = 新建。🔴 无 total_lessons（=课次数实时聚合）。
          lessons : 课次列表，每个 dict:{id?（空=新增）, lesson_seq:int, title, lesson_type:'0教学|1测试',
                    tag?（自由标签，吃透课走这）, source_ref?（素材源，如「学而思第10+11周」）,
                    thinking_action?（思维动作）, layer_target?（层数目标，如 '2→3'）,
                    parent_copy?（家长版口语文案）, kg_node_ids?:[str]（课内同步锚的 biz_subject id）,
                    seg_template?:list（本课次段模板，覆盖计划默认）}
        """
        try:
            client = await cluster.ensure_c()
            return await _upsert_course_plan(client, plan, lessons)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def schedule_sessions(
        target_type: str, target_id: str, items: list, plan_id: str = None,
        auto_bind: bool = True, force: bool = False,
    ) -> dict:
        """批量排课（给某对象铺一串场次）→ C 线 :8090。返回 {ok, created, conflicts}。

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
            client = await cluster.ensure_c()
            return await _schedule_sessions(client, target_type, target_id, items, plan_id, auto_bind, force)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def list_schedule(start: str, end: str, target_id: str = None) -> dict:
        """查某时间窗的月历场次（对象名/色、时间、课次标题、类型、备课态）→ C 线 :8090。返回 {ok, sessions}。

        参数:
          start     : 起始日期 'YYYY-MM-DD'（含）
          end       : 结束日期 'YYYY-MM-DD'（含）
          target_id : 只看某对象的场次（省略=我名下全部）
        """
        try:
            client = await cluster.ensure_c()
            return await _list_schedule(client, start, end, target_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def update_session(
        session_id: str, action: str, date: str = None, start: str = None, end: str = None,
        plan_lesson_id: str = None, note: str = None,
    ) -> dict:
        """改单场次（改期/请假/取消/标已上/锁内容/改绑课次/改备注）→ C 线 :8090。返回 {ok, deferred?, overflow?}。

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
            client = await cluster.ensure_c()
            return await _update_session(client, session_id, action, date, start, end, plan_lesson_id, note)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def build_prep_pack(lesson_id: str = None, session_id: str = None, segs: list = None) -> dict:
        """装配备课包（按段填题）→ C 线 :8090。返回 {ok, pack_id}。lesson_id/session_id 二选一（散课用 session_id）。

        备课包 = 一次课的分段题单（如 思维题 / 奥数专项 / 课内同步 三段）；1:1，已存在则返已有。
        参数:
          lesson_id  : 绑定的课次 id（计划内课次备课）——与 session_id 二选一
          session_id : 绑定的场次 id（散课/外部课直接对场次备课）
          segs       : 段列表 [{name:'段名', style:'风格描述', question_ids:[str]（🔴 字符串 id，防雪花截尾）,
                       rules?:str（分层规则，如 '第一层★7/第二层★★8/第三层★★★5选做'）,
                       note?:str（口诀/备注文本，专项段的核心口诀走这）}]
        """
        try:
            client = await cluster.ensure_c()
            return await _build_prep_pack(client, lesson_id, session_id, segs)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def render_prep_pack(pack_id: str, mark_ready: bool = True) -> dict:
        """把备课包逐段渲染成 PDF（一段一卷 A4，仅题目无解析）→ C 线 :8090。返回 {ok, artifacts}。

        🔴 段无题 → 整单报错不出半卷。全段成功且 mark_ready → pack/课次/场次 备课态置「已备好」。
        参数:
          pack_id    : 备课包 id
          mark_ready : True = 渲染成功后置备课态为已备好（默认 True）
        返回: {ok, artifacts:[{seg, file:服务端相对路径, pages, url:临时下载地址}]}。
        """
        try:
            client = await cluster.ensure_c()
            return await _render_prep_pack(client, pack_id, mark_ready)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def submit_review(
        session_id: str, item_results: list, teacher_note: str = None, parent_msg_override: str = None,
    ) -> dict:
        """课后回收（录逐题对错）→ 生成家长反馈 + 肖像增量 → C 线 :8090。返回 {ok, parent_msg, portrait_delta}。

        提交即标该场次「已上」。parent_msg 服务端模板拼装「家长您好！…思维题：/同步：/拓展奥数：」，
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
            client = await cluster.ensure_c()
            return await _submit_review(client, session_id, item_results, teacher_note, parent_msg_override)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def get_student_profile(target_id: str, target_type: str = "student") -> dict:
        """取对象详情 + 肖像（含 error_signals 易错库）→ C 线 :8090。返回 {ok, target, profile}。

        备课前读画像、看回收后新增的 pending 易错信号都走它。
        参数:
          target_id   : 对象 id（字符串）
          target_type : 'student'（默认）| 'class'
        """
        try:
            client = await cluster.ensure_c()
            return await _get_student_profile(client, target_id, target_type)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
