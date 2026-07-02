"""MCP 工具·写层(第二能力·录入)：format_question / ingest_question / upload_image + ingest_items（PRD-C-208 统一入库口）。

🔴 加第二能力=加这一个目录(tools/ingest.py)+ server.py register 三行；login/鉴权/底座调用层(ruoyi.py)零改 —— AC6 活证。
🔴 范式：Claude Code 读试卷(多模态)→ 拆题 + 抽题干/选项/答案/解析 + 选知识点("算"在 Claude)；
   MCP 工具只做确定性平台写：格式化成 blockJson(Java 确定性转换)、录题落库(事务多表)、图传 OSS。
🔴 teacher_id 由底座 LoginHelper.getUserId() 从登录 token 注入、不信 body → 录的题 create_by = 登录 teacher。
🔴 ingest_items = 七类来源统一入库口（IngestItem 契约见 README §契约面板）：一次调用完成
   题+图+知识点关系+打标字段（难度/易错/依据/模型链/场景/自由标签）+可选成卷；未知字段拒绝（extra=forbid 防契约漂移）；
   单题失败不回滚整批，results 逐条报告。模型链/难度依据走 app/db pymysql（3.3-b 拍板收口）。
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.dicts import ANNO_ERROR, ANNO_SCENE
from app.paperparse import FIG, FIG_IDS, dedup_key, infer_type, plain_text
from app.ruoyi import RuoyiClient, RuoyiError
from app.tools.compose import _standard_scores


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


# ───────────────────── PRD-C-208 契约面板：IngestItem（未知字段一律拒绝） ─────────────────────

class ItemImage(BaseModel):
    """IngestItem 的图：local_path（工具代传 OSS）或 ossUrl（已传好直用）。工具会把题面里的占位替换成图。

    题面占位可用两种写法，工具都能自动替换成 ![](ossUrl)：
      ① `〖图:rId4〗` 标记（convert_doc 原生产出）→ 配 images[].rid="rId4"；
      ② 字面 local_path 串（你自己拼进题面的）→ 配 images[].local_path（正/反斜杠均可）。
    未被任何图匹配的残留 〖图:...〗 标记会被自动清掉，不污染题面。
    """
    model_config = ConfigDict(extra="forbid")

    local_path: str = Field(default="", description="本地绝对路径（工具代传 OSS）")
    ossUrl: str = Field(default="", description="已有 OSS url（与 local_path 二选一）")
    assetId: Optional[int] = Field(default=None, description="image_asset id（配 ossUrl 时带上）")
    rid: str = Field(default="", description="convert_doc 的 〖图:rId〗 标记 id（如 rId4）；给了则自动把题面同 rid 标记替换成图")
    role: str = Field(default="stem", description="stem/figure/analysis")


class ModelRef(BaseModel):
    """既有解法模型引用 → biz_question_model。"""
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(description="biz_solution_model.id（如 TY07）")
    is_primary: int = Field(default=0, description="主模型 1/0")


class NewModel(BaseModel):
    """新模型提议 → biz_solution_model(status=2 待转正)。🔴 propose 串行执行（并发撞 TY 主键）。"""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="模型名（按 name 去重，已提议过则复用）")
    category: str = Field(default="通用")
    trigger_feature: str = Field(default="", description="触发特征")
    action_conclusion: str = Field(default="", description="动作/结论")
    difficulty_tier: int = Field(default=2, description="模型阶 1-4")
    freq_band: int = Field(default=1, description="考频档")
    is_primary: int = Field(default=0)


class IngestItem(BaseModel):
    """标准录入 JSON（七类来源殊途同归的唯一契约）。必填=stem；前置信息带则原样落库、不带留给打标。"""
    model_config = ConfigDict(extra="forbid")

    # ── 题面（必填组）──
    stem: str = Field(description="题干 markdown（可含 ![](ossUrl)/![](local_path) 占位、$LaTeX$、表格）")
    options: list[str] = Field(default=[], description="选项内容数组（非选择题=[]）")
    answer: str = Field(default="")
    analyze: str = Field(default="")
    question_type: Optional[int] = Field(default=None, description="1-8（app/dicts.py）；null=按内容推断")
    score: float = Field(default=0, description="分值，0=未知（成卷时未知按通值补）")
    images: list[ItemImage] = Field(default=[])
    # ── 前置信息（可选组）──
    kp_id: Optional[str] = Field(default=None, description="主考点（biz_subject 叶子 id，resolve_kg 查）")
    secondary_kps: list[str] = Field(default=[])
    difficult: Optional[int] = Field(default=None, description="1基础/2中等/3较难/4压轴；null=占位2留给打标")
    err: list[str] = Field(default=[], description="易错点（受控7词 biz_anno_ERROR）")
    why: str = Field(default="", description="难度一句依据 → biz_question_ai.difficulty_reason")
    models: list[ModelRef] = Field(default=[])
    new_models: list[NewModel] = Field(default=[])
    scenario: Optional[str] = Field(default=None, description="纯数学/现实生活/科学跨学科/数学文化")
    free_tags: list[str] = Field(default=[], description="自由知识标签 → biz_question_ai.tags（科学轻打标即此用法）")
    # ── 溯源（可选）──
    source_raw: str = Field(default="")
    exam_year: str = Field(default="")
    region_code: str = Field(default="", description="国标行政区划6位")
    source_type: int = Field(default=0, description="1中考/2模拟/3期末/4月考/5单元/6自编/9其他")
    external_key: Optional[str] = Field(default=None, description="幂等去重键；null=按题干规范化去重（现行为）")


class PaperSpec(BaseModel):
    """可选建卷；null=散题不成卷（3.3-c 拍板）。"""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="卷名")
    category_id: str = Field(default="", description="卷库目录节点 id（biz_paper_category）")
    total_score: int = Field(default=100)
    suggest_time: int = Field(default=40, description="建议时长(分钟)")


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

    @mcp.tool()
    async def ingest_items(
        items: list[IngestItem],
        subject_root: str,
        paper: Optional[PaperSpec] = None,
    ) -> dict:
        """🔴 统一入库口（七类来源殊途同归）：一次调用完成 题+图+知识点关系+打标字段+可选成卷，无需事后手补。

        每题依次：图代传 OSS（local_path→ossUrl 并替换题面占位）→ blockJson 格式化 → 录题落库
        （kp_id 走 knowledgeIds → 底座自动写 biz_question_knowledge + dim1_kp_id）→ 打标字段
        （err/scenario/free_tags → biz_question_ai；why → difficulty_reason；models/new_models → 模型链，批尾串行）。
        参数:
          items: IngestItem[]（契约见 README；未知字段拒绝，缺 stem 单条 fail 不中断批）
          subject_root: KG 教材根（数学七上="100"；科学="901".."906"）——无 kp_id 的题 subject_id 落此根
          paper: 可选建卷 {name, category_id, total_score, suggest_time}；null=散题不成卷
        前置信息原样落库不被覆盖（AC3）；同题干重复录入自动去重复用（AC4）。
        返回: {ok, results:[{num, question_id, created, reason?, warnings?}], paper_id?, stats:{ok,reused,fail,img}}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not subject_root:
            return {"ok": False, "reason": "subject_root 必填（数学七上=100 / 科学=901..906）"}
        if not items:
            return {"ok": False, "reason": "items 为空"}

        results = []
        n_ok = n_reused = n_fail = n_img = 0
        ok_qids: list[tuple[int, float]] = []      # (qid, item.score) 按成功序，供成卷+分值
        labeled_records: list[dict] = []           # {question_id, models, new_models} 批尾串行写模型链
        batch_key = (paper.name if paper else "") or f"items-{subject_root}"

        for idx, it in enumerate(items, 1):
            warnings: list[str] = []
            if not (it.stem or "").strip():
                n_fail += 1
                results.append({"num": idx, "reason": "缺 stem，整条 fail（不中断批）"})
                continue
            try:
                stem_md, analyze_md, answer_md = it.stem, it.analyze, it.answer
                opts_md = list(it.options or [])
                images_meta: list[dict] = []
                rid2oss: dict = {}      # convert_doc 〖图:rId〗 标记 → ossUrl
                # ── 图：local_path 代传 OSS + 占位替换；ossUrl 直用 ──
                for im in it.images or []:
                    oss, aid = im.ossUrl or None, im.assetId
                    if not oss and im.local_path:
                        up = await upload_image(im.local_path, "figure")
                        if not up.get("ok"):
                            warnings.append(f"图传失败({im.local_path}): {up.get('reason')}")
                            continue
                        oss, aid = up.get("oss_url"), up.get("asset_id")
                        n_img += 1
                        # 题面里的本地路径占位（正反斜杠两种写法）→ ossUrl
                        for raw in {im.local_path, im.local_path.replace("\\", "/")}:
                            stem_md = stem_md.replace(raw, oss)
                            analyze_md = analyze_md.replace(raw, oss)
                            opts_md = [o.replace(raw, oss) for o in opts_md]
                    if oss:
                        if im.rid:
                            rid2oss[im.rid] = oss
                        images_meta.append({"ossUrl": oss, "assetId": aid, "role": im.role or "stem"})

                # ── 〖图:rId〗 标记自动替换（convert_doc 原生产物直喂）：映射到的 rid → ![](oss)，
                #    未映射的 rid + 无 rid 的 〖图〗 一律清掉，不污染题面（AC7 verifier 反馈②根治）──
                def _imagify(s: str) -> str:
                    def rep(m):
                        out = ""
                        for r in [x.strip() for x in m.group(1).split(",")]:
                            if r in rid2oss:
                                out += f"![]({rid2oss[r]})"
                        return out
                    return FIG.sub("", FIG_IDS.sub(rep, s))
                if "〖图" in stem_md or "〖图" in analyze_md or any("〖图" in o for o in opts_md):
                    stem_md, analyze_md = _imagify(stem_md), _imagify(analyze_md)
                    opts_md = [_imagify(o) for o in opts_md]

                # ── 受控词表校验（选词别造铁律）：非法词剔除并 warning，合法词原样落 ──
                err_ok = [e for e in (it.err or []) if e in ANNO_ERROR]
                if len(err_ok) != len(it.err or []):
                    bad = [e for e in it.err if e not in ANNO_ERROR]
                    warnings.append(f"err 越词表被剔除: {bad}（受控7词={list(ANNO_ERROR)}）")
                scenario = it.scenario if (it.scenario in ANNO_SCENE) else None
                if it.scenario and not scenario:
                    warnings.append(f"scenario『{it.scenario}』越词表被忽略（{list(ANNO_SCENE)}）")

                qtype = it.question_type or infer_type(
                    stem_md, opts_md, None, "\n".join([stem_md, answer_md, analyze_md]))
                pure = plain_text(stem_md)
                ext_key = it.external_key or dedup_key(pure, batch_key, idx)

                # ── blockJson（确定性格式化，失败降级只存文本）──
                block_json = ""
                fmt = await format_question(qtype, stem_md, opts_md)
                if fmt.get("ok"):
                    block_json = fmt.get("block_json") or ""
                else:
                    warnings.append(f"format 降级: {fmt.get('reason')}")

                # ── 录题（kp 前置 → knowledgeIds，底座写关系表 + dim1_kp_id 列）──
                krefs = []
                if it.kp_id:
                    krefs.append(KnowledgeRef(kpId=str(it.kp_id), isPrimary=1))
                for s in it.secondary_kps or []:
                    krefs.append(KnowledgeRef(kpId=str(s), isPrimary=0))
                ing = await ingest_question(
                    subject_id=str(it.kp_id or subject_root),
                    question_type=qtype,
                    difficult=it.difficult or 2,     # null=占位2留给打标（现行为）
                    stem_text=pure or "（见原卷）",
                    block_json=block_json,
                    answer_text=answer_md,
                    analyze_text=analyze_md,
                    knowledge_ids=krefs,
                    images=[ImageRef(**m) for m in images_meta],
                    external_key=ext_key,
                    exam_year=it.exam_year,
                    region_code=it.region_code,
                    source_type=it.source_type,
                    source_raw=it.source_raw,
                    status="1",
                )
                if not ing.get("ok"):
                    n_fail += 1
                    results.append({"num": idx, "reason": ing.get("reason")})
                    continue
                qid = int(ing["question_id"])
                created = ing.get("created")
                n_ok += 1
                if created is False:
                    n_reused += 1
                ok_qids.append((qid, float(it.score or 0)))

                # ── 打标字段（带则一次写齐；不带留给打标流程）──
                if err_ok or it.free_tags or scenario or it.why:
                    ai_body: dict = {"labelStatus": 1, "labeledBy": "mcp-ingest-items", "needAnchorReview": 0}
                    if it.kp_id:
                        ai_body["anchorKpId"] = str(it.kp_id)
                    if err_ok:
                        ai_body["breakthroughPoints"] = err_ok   # 轻打标口径：易错 → breakthrough_points（同 sync_label）
                    if it.free_tags:
                        ai_body["tags"] = it.free_tags
                    if scenario:
                        ai_body["scenario"] = scenario
                    try:
                        await client.teacher_post(f"/teacher/ingest/ai/{qid}", ai_body)
                        if it.why:
                            from app import db
                            db.set_difficulty_reason(qid, it.why)
                    except (RuoyiError, Exception) as e:
                        warnings.append(f"打标字段落库失败: {e}")
                if it.models or it.new_models:
                    labeled_records.append({
                        "question_id": qid,
                        "models": [m.model_dump() for m in it.models or []],
                        "new_models": [m.model_dump() for m in it.new_models or []],
                    })

                r: dict = {"num": idx, "question_id": qid, "created": created}
                if warnings:
                    r["warnings"] = warnings
                results.append(r)
            except Exception as e:  # 单题任何异常不回滚整批
                n_fail += 1
                results.append({"num": idx, "reason": f"{type(e).__name__}: {e}"})

        # ── 模型链（批尾串行：先 propose 新模型回填，再写 biz_question_model）──
        models_note = ""
        if labeled_records:
            try:
                from app import db
                new_n, _ = db.propose_models(labeled_records)
                mdl_n = db.write_models(labeled_records)
                models_note = f"模型链+{mdl_n} 新模型+{new_n}"
            except Exception as e:
                models_note = f"模型链写入失败: {type(e).__name__}: {e}"

        # ── 可选成卷（3.3-c：不给 paper 则散题不成卷）──
        paper_id = None
        paper_note = ""
        if paper and ok_qids:
            try:
                body = {"name": paper.name, "questionIds": [q for q, _ in ok_qids]}
                if paper.category_id:
                    body["paperCategoryId"] = paper.category_id
                cp = await client.teacher_post("/teacher/exam/paper/create", body)
                paper_id = (cp or {}).get("paperId") or (cp or {}).get("id") or ((cp or {}).get("paper") or {}).get("id")
                if paper_id and paper.category_id:
                    from app import db
                    db.set_paper_subject(paper_id, paper.category_id)  # page() 按 subject_id 筛目录
                if paper_id:
                    paper_note = await _apply_scores(paper_id, ok_qids, paper.total_score, paper.suggest_time)
            except (RuoyiError, Exception) as e:
                paper_note = f"建卷失败: {e}"

        out = {
            "ok": n_fail == 0,
            "results": results,
            "stats": {"ok": n_ok, "reused": n_reused, "fail": n_fail, "img": n_img},
        }
        if paper_id:
            out["paper_id"] = paper_id
        note = "；".join(x for x in (models_note, paper_note) if x)
        if note:
            out["note"] = note
        return out

    async def _apply_scores(paper_id, ok_qids, total_score, suggest_time):
        """卷分值：items 带 score 则原样用（0 的小题默认 3）；全 0 则按年级标准分（compose._standard_scores）。"""
        detail = await client.teacher_post("/teacher/exam/paper/detail", {"paperId": int(paper_id)})
        flat = []
        for sec in (detail or {}).get("sections") or []:
            sid = sec.get("sectionId")
            for q in sec.get("questions") or []:
                flat.append((sid, int(q.get("id")), int(q.get("questionType") or 1)))
        if not flat:
            return "分值跳过（卷无题）"
        score_by_qid = {q: s for q, s in ok_qids}
        if any(s > 0 for _, s in ok_qids):
            scores = [score_by_qid.get(qid, 0) or 3 for _, qid, _t in flat]
        else:
            scores = _standard_scores([t for _, _, t in flat], int(total_score))
        sec_seq: dict = {}
        questions = []
        for (sid, qid, _t), sc in zip(flat, scores):
            sec_seq[sid] = sec_seq.get(sid, 0) + 1
            questions.append({"questionId": qid, "sectionId": sid, "sort": sec_seq[sid], "score": sc})
        await client.teacher_post("/teacher/exam/paper/update", {
            "paperId": int(paper_id), "questions": questions, "suggestTime": int(suggest_time)})
        return f"建卷 paper_id={paper_id} 总分{int(sum(scores))} 时长{suggest_time}min"
