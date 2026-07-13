"""MCP 工具·备课组专项（PRD-003 三工具草稿）——集成段由调度中心并入 teacher-mcp。

三工具（agent 全程代理"跨书组专项 → 双卷 PDF → 绑课次"）：
  compose_special       跨书选料建专项 + 批量挑题入区块（book_type='special'，复用 biz_shelf_* 三表）
  export_special        双卷 PDF 导出（题目卷/答案卷，含解析/星标开关；导出即 used_count+1）
  bind_special_to_lesson 专项 ↔ 课次材料位（🔴 只 UPDATE special_ids 单列，不碰 paper_slots）

设计对齐（与现网 prep.py 同构）：
  - 模块级 async 纯函数无（本组直调 BE，逻辑薄）；register(mcp, client) 注入单 RuoyiClient（:9090）。
  - 所有写工具先 client.has_session() 检查；未登录返 {ok:false, reason:"需先 login"}。
  - id 全链路字符串传（雪花号 JSON double 截尾坑，C-001）；BE 端点全 /teacher/special/**。
  - tags={"prep"}：随备课角色暴露（组专项是备课闭环②③步）。

对接 BE（codeplace-C book-server line-C，SpecialController）：
  POST /teacher/special                         建专项 → {id}
  POST /teacher/special/{id}/pick               挑题 {questionId?|nodeId?, overrideJson?, secHint?}
  GET  /teacher/special/{id}                     全树详情
  POST /teacher/special/{id}/export             双卷导出 {papers, withAnalysis, withStars} → {questionUrl, answerUrl, markedCount}
  POST /teacher/special/lesson/{lid}/bind        绑课次 {specialId}
  POST /teacher/special/lesson/{lid}/unbind      解绑 {specialId}
  GET  /teacher/special/lesson/{lid}/materials   查课次材料
"""
from typing import Optional

from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/special"


class PickItem(BaseModel):
    """一次挑题：questionId=单题入区块；nodeId=按源节点整段批量复制入专项。二者择一。"""

    questionId: Optional[str] = Field(default=None, description="题库题 id（字符串，来自 search_questions/书浏览）")
    nodeId: Optional[str] = Field(default=None, description="源节点 id（整节批量入，questionId 省略时用）")
    secHint: str = Field(default="", description="落入的区块名（不存在则新建 sec，如'有理数运算'）")
    overrideJson: Optional[dict] = Field(default=None, description="题面覆盖（改编题干/答案/留空 __gap 等），不改题库源题")


def register(mcp, client: RuoyiClient) -> None:

    # ═════════════ 1. compose_special：跨书选料建专项 ═════════════
    @mcp.tool(tags={"prep"})
    async def compose_special(title: str, picks: list[PickItem], grade: str = "",
                              subject_id: str = "") -> dict:
        """跨多本书选料，一单建成一个"专项"（book_type='special'）并批量挑题入区块。

        专项 = word/教辅式文档（非试卷）：结构 = 区块(sec) → 难度档(tier) → 题。挑题必在
        区块框架下（secHint 指定落点，缺则新建）。跨书 = picks 里的题可来自不同源书，
        专项只引用题库题 id，不动源书与题库（源书 item 数、题库 stem 全程不变）。

        参数:
          title:   专项名（卷面可见，🔴 只写干净知识点名，绝不含内部词 层/素材/薄弱/★）。
          picks:   [{questionId?|nodeId?, secHint?, overrideJson?}, ...]，逐条挑题。
          grade / subject_id: 可选元信息。
        返回:
          {ok, special_id, picked, secs:[{secId,name}], skipped}；未登录/空 picks → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        valid = [p for p in picks if p.questionId or p.nodeId]
        if not valid:
            return {"ok": False, "reason": "picks 为空或无有效项（每项需 questionId 或 nodeId）"}
        try:
            created = await client.teacher_post(BASE, {"title": title, "grade": grade,
                                                       "subjectId": subject_id})
        except RuoyiError as e:
            return {"ok": False, "reason": f"建专项失败: {e}"}
        special_id = str((created or {}).get("id") or "")
        if not special_id:
            return {"ok": False, "reason": "建专项未返回 id", "raw": created}

        picked, skipped, secs = 0, [], {}
        for p in valid:
            body: dict = {"secHint": p.secHint or ""}
            if p.questionId:
                body["questionId"] = str(p.questionId)
            if p.nodeId:
                body["nodeId"] = str(p.nodeId)
            if p.overrideJson:
                body["overrideJson"] = p.overrideJson
            try:
                r = await client.teacher_post(f"{BASE}/{special_id}/pick", body)
            except RuoyiError as e:
                skipped.append({"pick": body, "error": str(e)})
                continue
            sec_id = str((r or {}).get("secId") or "")
            added = (r or {}).get("addedCount", 1)
            picked += int(added or 0) if added else 1
            if sec_id:
                secs.setdefault(sec_id, {"secId": sec_id, "name": p.secHint or ""})
        return {"ok": True, "special_id": special_id, "picked": picked,
                "secs": list(secs.values()), "skipped": skipped}

    # ═════════════ 2. export_special：双卷 PDF 导出 ═════════════
    @mcp.tool(tags={"prep"})
    async def export_special(special_id: str, papers: Optional[list[str]] = None,
                             with_analysis: bool = True, with_stars: bool = False) -> dict:
        """把专项导出成题目卷 / 答案卷双 PDF（苏俊宇卷版式，HTML→无头 Chrome→PDF）。

        导出即对专项内每道 item used_count+1（认证计数=拿去上课的信号，不可逆软计数）。
        🔴 卷面纪律：★ 仅 with_stars=True 显示（默认隐藏）；【解析】仅 with_analysis=True 附带；
           卷面绝不出现内部词（层/素材/薄弱）。

        参数:
          special_id:    专项 id（字符串）。
          papers:        ['question','answer'] 任子集，缺省两卷都出。
          with_analysis: 答案卷是否含解析（默认 True）。
          with_stars:    是否显示难度星标（默认 False=隐藏）。
        返回:
          {ok, special_id, question_url?, answer_url?, marked_count}；空专项(无题) → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        papers = papers or ["question", "answer"]
        bad = [p for p in papers if p not in ("question", "answer")]
        if bad:
            return {"ok": False, "reason": f"papers 非法项 {bad}（仅 'question'/'answer'）"}
        body = {"papers": papers, "withAnalysis": bool(with_analysis), "withStars": bool(with_stars)}
        try:
            r = await client.teacher_post(f"{BASE}/{special_id}/export", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"导出失败: {e}（空专项/未装 Chrome 常见）"}
        r = r or {}
        return {"ok": True, "special_id": str(special_id),
                "question_url": r.get("questionUrl"),
                "answer_url": r.get("answerUrl"),
                "marked_count": r.get("markedCount", 0)}

    # ═════════════ 3. bind_special_to_lesson：专项↔课次材料位 ═════════════
    @mcp.tool(tags={"prep"})
    async def bind_special_to_lesson(lesson_id: str, special_id: str, action: str = "bind") -> dict:
        """把专项绑到课次材料位（D4 持久绑定），或解绑，或查本课已绑材料。

        🔴 只 UPDATE biz_course_plan_lesson.special_ids 单列——绝不整行 upsert（历史事故：
           整行重写把 paper_slots 已绑 paper_id 抹掉）。BE 端 partial updateById 只写 special_ids。

        参数:
          lesson_id:  课次 id（字符串）。
          special_id: 专项 id（action=materials 时忽略）。
          action:     'bind'（默认）/ 'unbind' / 'materials'（查本课已绑专项概要）。
        返回:
          bind/unbind → {ok, lesson_id, special_ids:[...]}；
          materials   → {ok, lesson_id, special_ids:[...], specials:[{id,title,itemCount}]}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        act = (action or "bind").strip().lower()
        try:
            if act == "materials":
                r = await client.teacher_get(f"{BASE}/lesson/{lesson_id}/materials")
                r = r or {}
                return {"ok": True, "lesson_id": str(lesson_id),
                        "special_ids": [str(x) for x in (r.get("specialIds") or [])],
                        "specials": r.get("specials") or []}
            if act not in ("bind", "unbind"):
                return {"ok": False, "reason": f"未知 action: {action}（bind/unbind/materials）"}
            r = await client.teacher_post(f"{BASE}/lesson/{lesson_id}/{act}", {"specialId": str(special_id)})
            r = r or {}
            return {"ok": True, "lesson_id": str(lesson_id),
                    "special_ids": [str(x) for x in (r.get("specialIds") or [])]}
        except RuoyiError as e:
            return {"ok": False, "reason": f"{act} 失败: {e}"}
