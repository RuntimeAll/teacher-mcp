"""MCP 工具·打标层（第三能力·DNA 打标）：label_question。

🔴 加能力 = 加这一个目录(tools/label.py) + server.py register 一行；login/鉴权/底座调用层(ruoyi.py)零改（AC6）。
🔴 范式：Claude Code 读题（题干+答案+解析+配图，多模态）+ KG 上下文 → 判难度(★1-4 rubric)/选知识点锚/抽 DNA（"算"在 Claude）；
   MCP 只确定性写：DNA blob 走 /teacher/ingest/ai，结构列(难度/dim1锚/dim5/labelStatus)走 /teacher/question/update-attrs。
🔴 难度不靠 LLM 自评——按「难度评级」skill ★1-4 规律评；知识点锚同版本精确选（不跨版本近似）。
🔴 受控词表（app/dicts.py）：hard_points∈biz_anno_ERROR、scenario∈biz_anno_SCENE，选词别造。
🔴 两端点都已挂 xss.excludeUrls（数学 < > 不被剥）；都走 /teacher/** misikt envelope。
"""
from typing import Any, Optional

from app.ruoyi import RuoyiClient, RuoyiError


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool()
    async def label_question(
        question_id: int,
        difficult: int,
        dim1_kp_id: str = "",
        anchor_confidence: Optional[float] = None,
        need_anchor_review: int = 0,
        dim5_structure: str = "",
        solution_skeleton: str = "",
        assessment_type: str = "",
        hard_points: list[str] = [],
        breakthrough_points: list[str] = [],
        tags: list[str] = [],
        scenario: str = "",
        dna_type: str = "",
        parametric_slots: list[Any] = [],
        modeling_frame: Optional[Any] = None,
        conditions: Optional[Any] = None,
        variation_profile: Optional[Any] = None,
    ) -> dict:
        """给一道已入库的题写 DNA 打标（难度 + 知识点锚 + 解法骨架 + 变式底料），归当前登录 teacher。

        前置：题已 ingest_question 入库（有题干/答案/解析/配图）。Claude 先读题(多模态)+本章 KG 上下文判好，再调本工具落库。
        参数（必填 = question_id, difficult；其余按题打满 / 基础题留空）：
          question_id  : biz_question.id
          difficult    : ★难度 1基础/2中等/3较难/4压轴（按「难度评级」rubric 判档，非 LLM 自评）
          dim1_kp_id   : 知识点锚叶子 id（同版本精确锚，如浙教七上根 100 下的叶子；不跨版本近似，对不上就留空+need_anchor_review=1）
          anchor_confidence: 锚定置信 0-1
          need_anchor_review: 锚存疑待人审 1/0
          dim5_structure: 图形/情境结构指纹
          solution_skeleton: 解法骨架（步骤序列，【】标最难步）—— 撑变式①数值②结构算子
          assessment_type: 考察类型
          hard_points  : 难点[]（受控词表 biz_anno_ERROR：概念混淆/计算失误/审题偏差/隐含遗漏/分类不全/表达不规范/思路缺失）
          breakthrough_points: 突破点[]（★1/★2 送分/常规题可空）
          tags         : 检索标签 3-6（召回用）
          scenario     : 场景（仅应用题；受控词表 biz_anno_SCENE：纯数学/现实生活/科学跨学科/数学文化）
          dna_type     : DNA 类型
          parametric_slots / modeling_frame / conditions / variation_profile: 变式底料（母题打满，普通题可简，应用题才有 modeling_frame）
        返回: {ok, question_id, ai_id, difficult, dim1_kp_id}；异常 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not question_id:
            return {"ok": False, "reason": "question_id 必填"}
        if difficult not in (1, 2, 3, 4):
            return {"ok": False, "reason": "difficult 必须 1-4（★1基础/2中等/3较难/4压轴）"}

        qid = int(question_id)  # pydantic 已把字符串 id 无损转 int；底座 Long 精度足
        # ① 结构列 + 存在校验 → /teacher/question/update-attrs（难度/dim1锚/dim4/dim5/labelStatus）。
        #    🔴 放最前：update-attrs 会 selectById 校验题存在，题不存在直接失败 → 不写孤儿 DNA
        #    （snowflake 19 位 id 经 JSON double 截断必踩，先校验挡住）。
        attrs_body: dict = {
            "questionId": qid,
            "difficult": difficult,
            "dim4Difficulty": difficult,
            "labelStatus": 1,
        }
        if dim1_kp_id:
            attrs_body["dim1KpId"] = dim1_kp_id
        if anchor_confidence is not None:
            attrs_body["labelConfidence"] = anchor_confidence
        if dim5_structure:
            attrs_body["dim5Structure"] = dim5_structure
        try:
            await client.teacher_post("/teacher/question/update-attrs", attrs_body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"难度/锚(update-attrs)失败（题可能不存在/id 精度）: {e}"}

        # ② DNA blob → /teacher/ingest/ai/{qid}（biz_question_ai 全量，含变式基因；与 ① 同一行 upsert）
        ai_body: dict = {"labelStatus": 1, "needAnchorReview": need_anchor_review}
        if dim1_kp_id:
            ai_body["anchorKpId"] = dim1_kp_id
        if anchor_confidence is not None:
            ai_body["anchorConfidence"] = anchor_confidence
        for k, v in (
            ("solutionSkeleton", solution_skeleton), ("assessmentType", assessment_type),
            ("scenario", scenario), ("dnaType", dna_type),
        ):
            if v:
                ai_body[k] = v
        for k, v in (
            ("hardPoints", hard_points), ("breakthroughPoints", breakthrough_points),
            ("tags", tags), ("parametricSlots", parametric_slots),
        ):
            if v:
                ai_body[k] = v
        if modeling_frame is not None:
            ai_body["modelingFrame"] = modeling_frame
        if conditions is not None:
            ai_body["conditions"] = conditions
        if variation_profile is not None:
            ai_body["variationProfile"] = variation_profile
        try:
            ai_resp = await client.teacher_post(f"/teacher/ingest/ai/{qid}", ai_body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"难度/锚已写但 DNA(/ingest/ai)失败: {e}"}
        ai_id = (ai_resp or {}).get("aiId")
        return {
            "ok": True, "question_id": question_id, "ai_id": ai_id,
            "difficult": difficult, "dim1_kp_id": dim1_kp_id or None,
        }
