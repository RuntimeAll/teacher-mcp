"""MCP 工具·写层(第二能力·录入)：format_question / ingest_question / upload_image。

🔴 加第二能力=加这一个目录(tools/ingest.py)+ server.py register 三行；login/鉴权/底座调用层(ruoyi.py)零改 —— AC6 活证。
🔴 范式：Claude Code 读试卷(多模态)→ 拆题 + 抽题干/选项/答案/解析 + 选知识点("算"在 Claude)；
   MCP 工具只做确定性平台写：格式化成 blockJson(Java 确定性转换)、录题落库(事务多表)、图传 OSS。
🔴 teacher_id 由底座 LoginHelper.getUserId() 从登录 token 注入、不信 body → 录的题 create_by = 登录 teacher。
"""
from typing import Optional

from pydantic import BaseModel, Field

from app.ruoyi import RuoyiClient, RuoyiError


class KnowledgeRef(BaseModel):
    """锚知识点叶子 → biz_question_knowledge。kpId 取自 list_kg_tree 的叶子 id。"""

    kpId: str = Field(description="知识点 biz_subject.id 叶子（来自 list_kg_tree）")
    isPrimary: int = Field(default=1, description="是否主考点 1/0")
    source: str = Field(default="U", description="U=用户 / S=标准库")
    confidence: Optional[float] = Field(default=None, description="置信度 0-1，可空")


class ImageRef(BaseModel):
    """题图引用 → biz_question_image。ossUrl 取自 upload_image 返回。"""

    ossUrl: str = Field(description="图 OSS url（upload_image 返回）")
    assetId: Optional[int] = Field(default=None, description="image_asset id（upload_image 返回）")
    blockId: str = Field(default="", description="对应 blockJson 里图块的 id，可空")
    role: str = Field(default="stem", description="stem/answer/figure")
    seq: int = Field(default=0, description="同 role 内顺序")
    isDecorative: int = Field(default=0, description="是否装饰图 1/0")


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool()
    async def format_question(question_type: int, stem: str, options: list[str] = []) -> dict:
        """把自然 markdown 题干 + 选项数组确定性转成 blockJson（三端统一渲染格式）。底座永不抛、识别不了降级 markdown 块。

        Claude 只产最小内容：markdown 题干（可含小问（1）（2）、图标记 ![](url)、$LaTeX$、表格）+ 选项内容数组（label 自动 A/B/C…）。
        参数: question_type 见字典 biz_question_type（1选择/2判断/3应用/4填空/5解答/6作图/7计算/8证明）；options 仅选择题非空。
        返回: {ok, block_json, degraded?}。把 block_json 喂给 ingest_question。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            resp = await client.teacher_post(
                "/teacher/format/to-block",
                {"type": question_type, "stem": stem, "options": options or []},
            )
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        resp = resp or {}
        return {"ok": True, "block_json": resp.get("blockJson"), "degraded": resp.get("degraded", False)}

    @mcp.tool()
    async def upload_image(local_path: str, asset_kind: str = "figure") -> dict:
        """把本地磁盘图片直传 OSS + 去重，返回可塞进 blockJson 图块 / ingest_question.images 的 ossUrl。

        参数: local_path 本地绝对路径；asset_kind 资产类型（figure 等）。
        返回: {ok, asset_id, oss_url, dedup}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            resp = await client.teacher_post(
                "/teacher/ingest/image", {"localPath": local_path, "assetKind": asset_kind}
            )
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        resp = resp or {}
        return {
            "ok": True,
            "asset_id": resp.get("assetId"),
            "oss_url": resp.get("ossUrl"),
            "dedup": resp.get("dedup"),
        }

    @mcp.tool()
    async def ingest_question(
        subject_id: str,
        question_type: int,
        difficult: int,
        stem_text: str,
        block_json: str = "",
        answer_text: str = "",
        analyze_text: str = "",
        knowledge_ids: list[KnowledgeRef] = [],
        images: list[ImageRef] = [],
        external_key: str = "",
        exam_year: str = "",
        region_code: str = "",
        source_type: int = 0,
        source_raw: str = "",
        status: str = "1",
    ) -> dict:
        """录一道题入库（事务多表），归属当前登录 teacher。返回 {ok, question_id, created}。

        参数（NOT NULL=必填）:
          subject_id  : 科目锚 level1（年级根，如 数学七上 的根 id），NOT NULL
          question_type: 字典 biz_question_type 1选择/2判断/3应用/4填空/5解答/6作图/7计算/8证明，NOT NULL
          difficult   : 1基础/2提升/3压轴，NOT NULL
          stem_text   : 纯文本题干（全文检索 + 去重 hash），NOT NULL
          block_json  : 来自 format_question 的 blockJson（三端渲染）；空则只存 stem_text
          answer_text / analyze_text: 答案 / 解析文本
          knowledge_ids: 锚知识点叶子 [{kpId, isPrimary, source, confidence}]（KG 关联，供组卷/举一反三召回）
          images      : 题图 [{ossUrl, assetId, role, ...}]（来自 upload_image）
          external_key: 幂等键（book+节+课时+题号），去重；空则按 stem_text hash 去重
          status      : '0'草稿 / '1'发布（默认发布）
        异常: 底座报错 → {ok:false, reason}（不假成功）。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not (subject_id and stem_text):
            return {"ok": False, "reason": "subject_id 与 stem_text 必填"}

        body = {
            "subjectId": subject_id,
            "questionType": question_type,
            "difficult": difficult,
            "stemText": stem_text,
            "status": status,
        }
        if block_json:
            body["blockJson"] = block_json
        if answer_text:
            body["answerText"] = answer_text
        if analyze_text:
            body["analyzeText"] = analyze_text
        if knowledge_ids:
            body["knowledgeIds"] = [k.model_dump(exclude_none=True) for k in knowledge_ids]
        if images:
            body["images"] = [im.model_dump(exclude_none=True) for im in images]
        if external_key:
            body["externalKey"] = external_key
        if exam_year:
            body["examYear"] = exam_year
        if region_code:
            body["regionCode"] = region_code           # 金标·地点（国标行政区划）
        if source_type:
            body["sourceType"] = source_type            # 类型 1中考/2模拟/3期末/4月考/5单元/6自编/9其他
        if source_raw:
            body["sourceRaw"] = source_raw

        try:
            resp = await client.teacher_post("/teacher/ingest/question", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"录题失败: {e}"}
        resp = resp or {}
        qid = resp.get("questionId")
        if not qid:
            return {"ok": False, "reason": "录题接口未返回 questionId", "raw": resp}
        return {"ok": True, "question_id": qid, "created": resp.get("created")}
