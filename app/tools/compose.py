"""MCP 工具·写层(探针能力)：compose_paper（确定性组卷 + 真落库 biz_paper）。

🔴 数据归 RuoYi、计算归编排层：意图解析/选知识点由 Claude Code 做，本工具只确定性拼参 + 调底座落库。
🔴 真落库走 save=true + teacherId(=当前登录会话 userId) → biz_paper.create_by 归属该 teacher。
"""
from typing import Optional

from pydantic import BaseModel, Field

from app.ruoyi import RuoyiClient, RuoyiError


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


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool()
    async def compose_paper(outline: list[OutlineItem], title: str = "") -> dict:
        """按大纲从真题库确定性组卷并真落库 biz_paper，归属当前登录 teacher。

        参数:
          outline: [{subjectId, subjectName?, questionType, difficult, count}, ...]
                   subjectId 取自 list_kg_tree 的叶子 id；编排层负责选点，本工具不二次解析意图。
          title:   卷名（可选，默认"MCP组卷"）。
        返回:
          {ok, paper_id, item_count, paper, notes}；底座不在/无匹配题 → {ok:false, reason}（不假成功）。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
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

    @mcp.tool()
    async def create_paper(name: str, question_ids: list[int], paper_category_id: str = "") -> dict:
        """按指定题目 id 列表（顺序即试卷内题号顺序）组装成一套**试卷**入卷库，归属当前登录 teacher。

        用于整卷录入：题目已 ingest_question 入库后，把它们按原卷题号顺序串成 biz_paper（建 section + biz_paper_question 关联）。
        参数:
          name: 试卷名（如原卷标题），1-200 字符。
          question_ids: 题目 id 列表，**顺序 = 试卷内题号顺序**，至少 1 题。
          paper_category_id: 试卷分类 id（可选，卷库目录树；空=根级）。
        返回: {ok, paper_id, ...}；异常 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not name or not question_ids:
            return {"ok": False, "reason": "name 与 question_ids 必填"}
        body = {"name": name, "questionIds": [int(q) for q in question_ids]}
        if paper_category_id:
            body["paperCategoryId"] = paper_category_id
        try:
            resp = await client.teacher_post("/teacher/exam/paper/create", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"建卷失败: {e}"}
        resp = resp or {}
        pid = resp.get("paperId") or resp.get("id") or (resp.get("paper") or {}).get("id")
        if not pid:
            return {"ok": False, "reason": "建卷接口未返回 paperId", "raw": resp}
        return {"ok": True, "paper_id": pid, "name": name, "question_count": len(question_ids)}

    @mcp.tool()
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
