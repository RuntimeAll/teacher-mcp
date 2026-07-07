"""MCP 工具·举一反三组（variant 角色，tags={"variant"}）—— PRD-O-005 批3 契约 v2。

薄代理 toolkit 举一反三端点（H2 配方实测），多轮有状态编排；智能决策（挑章节/改题内容）
留给驱动 agent，MCP 只做确定性透传 + 编排信号。铁律：
  - 入口只认图片 URL；D8 方案 A「渲图旁路」：纯文本题（无图）由 MCP 确定性渲图→传 OSS→喂引擎，
    不再软拒绝（question_id 无图 / stem_text 直传均走此路，返回 rendered_stem:true）。
  - 每次 invoke 必带真实 RuoYi token（ToolkitClient 从登录态注入 agent_config.ruoyi_token）。
  - id 全链路字符串（雪花截尾坑）；异常一律 {ok:false, error, hint}，绝不抛协议级异常。
  - persist 前须 verify（说明书铁律）；配图可 dsl 覆盖重绘。
"""
import uuid
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError
from teacher_mcp.backends.toolkit import (
    ToolkitAuthError,
    ToolkitClient,
    ToolkitDownError,
    ToolkitError,
)

_TK_DOWN_HINT = "toolkit(:9093) 未起，起法=start-dev -Part tk"
_LOGIN_HINT = "先调 login 工具（举一反三入口需真实 RuoYi 登录态）"
_RENDER_FAIL_HINT = "渲图旁路失败（题干渲染或传 OSS 未成）——见 error；可改传 image_url 走带图路径"


def _err(e: Exception) -> dict:
    """异常 → 语义化 {ok:false, error, hint}。绝不外抛。"""
    if isinstance(e, ToolkitDownError):
        return {"ok": False, "error": str(e), "hint": _TK_DOWN_HINT}
    if isinstance(e, ToolkitAuthError):
        return {"ok": False, "error": str(e), "hint": _LOGIN_HINT}
    if isinstance(e, RuoyiError):
        return {"ok": False, "error": str(e), "hint": "查题/登录底座(:9090)异常，确认已 login 且 BE 在跑"}
    if isinstance(e, ToolkitError):
        return {"ok": False, "error": str(e), "hint": "toolkit 端点返回异常，核对 thread_id 是否存在"}
    return {"ok": False, "error": f"{type(e).__name__}: {e}", "hint": "未预期异常，见 error"}


def _content_text(msg) -> str:
    """ChatMessage.content → 纯文本（content 可为 str 或结构化 list[{text|artifact}]）。"""
    if isinstance(msg, dict):
        c = msg.get("content")
    else:
        c = msg
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for seg in c:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict) and isinstance(seg.get("text"), str):
                parts.append(seg["text"])
        return "\n".join(parts)
    return str(c or "")


def _new_thread() -> str:
    return f"mcp-var-{uuid.uuid4().hex}"


def register(mcp, ruoyi_client: RuoyiClient, toolkit_client: ToolkitClient) -> None:
    tk = toolkit_client

    # ───────────────────────── 内部编排辅助 ─────────────────────────
    async def _artifact(thread_id: str) -> dict:
        """取会话 artifact 快照 {items, header}（读同一 thread checkpointer 状态）。"""
        return await tk.variant_call("/variant/artifact", {"thread_id": thread_id})

    def _find_item(art: dict, item_id: str) -> Optional[dict]:
        """按 item_id（= seq，字符串）在 artifact.items 里定位一题；未命中按 1-based index 兜底。"""
        items = (art or {}).get("items") or []
        target = str(item_id).strip()
        for it in items:
            if str(it.get("seq")) == target:
                return it
        for it in items:
            if str(it.get("index")) == target:
                return it
        return None

    def _map_variant(it: dict) -> dict:
        """artifact item → 契约 v2 变式行（id 字符串）。"""
        seq = it.get("seq") if it.get("seq") is not None else it.get("index")
        return {
            "item_id": str(seq),
            "seq": seq,
            "stem": it.get("stem") or "",
            "answer": it.get("answer") or "",
            "solution": it.get("solution") or "",
            "qtype": it.get("qtype") or "",
            "difficulty": it.get("difficulty"),
            "level": it.get("level"),
            "tier": it.get("tier"),
            "verify_status": it.get("verify_status"),
            "gene": it.get("gene"),
            "persisted": bool(it.get("persisted")),
            "question_id": (str(it["question_id"]) if it.get("question_id") else None),
            "figure_spec": it.get("figure_spec"),
            "figure_url": it.get("figure_url"),
            "dna": it.get("dna"),
        }

    async def _resolve_image_url(question_id: str) -> Optional[str]:
        """question_id → 图 oss_url。先 HTTP 查题（确认存在 + stemImg），空则查 biz_question_image。"""
        qid = str(question_id).strip()
        stem_img = None
        try:
            resp = await ruoyi_client.teacher_get("/teacher/question/list", {"ids": qid})
            rows = resp if isinstance(resp, list) else (resp.get("list") or resp.get("rows") or [])
            if rows and isinstance(rows[0], dict):
                stem_img = rows[0].get("stemImg") or rows[0].get("stemImgUrl")
        except RuoyiError:
            stem_img = None  # HTTP 侧拿不到不致命，落 DB 兜底
        if stem_img and str(stem_img).startswith("http"):
            return str(stem_img)
        # 现实数据：stemImg 常空，图只在 biz_question_image → DB 只读兜底
        from teacher_mcp.backends import db
        return db.question_image_url(qid)

    async def _fetch_stem(question_id: str) -> str:
        """question_id → 纯文本题干（stemText）。查 /teacher/question/list，取第一行 stemText。"""
        qid = str(question_id).strip()
        resp = await ruoyi_client.teacher_get("/teacher/question/list", {"ids": qid})
        rows = resp if isinstance(resp, list) else (resp.get("list") or resp.get("rows") or [])
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("stemText") or "").strip()
        return ""

    async def _render_and_upload(stem: str) -> str:
        """题干渲图旁路：render_stem → 落 tempfile → upload_image 传 OSS → 返回 oss_url。

        渲染或上传任一环失败 → 抛异常（由 make_variants 转 _RENDER_FAIL_HINT 软拒绝）。
        """
        from teacher_mcp.domains.stemrender import render_stem
        rendered = render_stem(stem)
        if not rendered.get("ok") or not rendered.get("path"):
            raise ToolkitError(f"题干渲图失败: {rendered.get('error') or '未产出图片'}")
        path = rendered["path"]
        try:
            resp = await ruoyi_client.teacher_post(
                "/teacher/ingest/image", {"localPath": path, "assetKind": "figure"}
            )
            oss_url = (resp or {}).get("ossUrl")
            if not oss_url or not str(oss_url).startswith("http"):
                raise ToolkitError(f"渲出图传 OSS 无 ossUrl: {resp}")
            return str(oss_url)
        finally:
            try:
                import os
                os.remove(path)
            except OSError:
                pass

    # ───────────────────────── 1. 母题轮 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def make_variants(
        question_id: str = "", image_url: str = "", stem_text: str = "", hint: str = "",
        count: int = 3, thread_id: str = "",
    ) -> dict:
        """举一反三·母题轮：读图解题打标 → 出母题卡（LLM 轮 ~60s）。图驱动 + D8 渲图旁路。

        入参三选一（image_url > stem_text > question_id 优先级）：
          - image_url : 公网可达图片 URL（.png/.jpg/.jpeg/.webp）——直接喂入口。
          - stem_text : 纯文本题干（可含 $LaTeX$/markdown）→ MCP 确定性渲图→传 OSS→喂引擎（rendered_stem:true）。
          - question_id: 题库题 id（字符串）→ 先查 biz_question_image 的 oss_url；
                         **无图则走渲图旁路**（取该题 stemText 渲图→传 OSS），返回 rendered_stem:true。
          - hint      : 追加指令（如「侧重折叠」），默认「帮我把这道题举一反三」。
          - count     : 变式数（进 message 文案，实际生成在 generate_variants）。
          - thread_id : 续跑同一母题会话用；缺省自动生成（uuid4）。返回值里带回，后续工具必传它。
        返回: {ok, thread_id, status:"ready"|"need_confirm", mother_card, kg_candidates?, reply, rendered_stem?}。
          - rendered_stem=true ⇒ 母题图由渲图旁路生成（题干确定性渲染，opus 读图 OCR）。
          - status=need_confirm（低置信/骨架空分支）→ 读 kg_candidates 挑章 → confirm_variant_chapter。
          - status=ready → 直接 generate_variants。
          - mother_card=None（入口回催图/催登录）→ ok:false，hint=引擎回文（reply）。
        """
        try:
            tk.require_token()  # 早失败给清晰 login 提示
            url = (image_url or "").strip()
            rendered_stem = False
            if not url:
                stem = (stem_text or "").strip()
                if not stem and not (question_id or "").strip():
                    return {"ok": False, "error": "需 question_id / image_url / stem_text 其一",
                            "hint": "带图题传 image_url；纯文本题传 stem_text 或 question_id（自动渲图旁路）"}
                if not stem:
                    # question_id 路线：优先取真图；无图则取 stemText 走渲图旁路
                    url = await _resolve_image_url(question_id)
                    if not url:
                        stem = await _fetch_stem(question_id)
                        if not stem:
                            return {"ok": False, "error": f"题 {question_id} 既无图也无题干文本",
                                    "hint": "该题 stemText 为空，无法渲图旁路；核对题 id"}
                if not url:
                    # stem_text 直传 或 question_id 无图 → 渲图旁路
                    try:
                        url = await _render_and_upload(stem)
                        rendered_stem = True
                    except Exception as e:  # noqa: BLE001
                        return {"ok": False, "error": f"{type(e).__name__}: {e}", "hint": _RENDER_FAIL_HINT}
            tid = (thread_id or "").strip() or _new_thread()
            message = f"{hint or '帮我把这道题举一反三'}，出{int(count)}道变式 {url}"
            resp = await tk.invoke(message, tid)
            reply = _content_text(resp)
            art = await _artifact(tid)
            header = (art or {}).get("header") or {}
            mother_card = header.get("mother_card")
            mconfirm = header.get("mother_confirm") or {}
            if not mother_card:
                # 入口未产出母题卡（催图/催登录/图不可达）——把引擎回文透出给驱动 agent
                return {"ok": False, "thread_id": tid, "error": "未产出母题卡",
                        "rendered_stem": rendered_stem,
                        "hint": reply[:400] or "入口未识别图片，确认 URL 公网可达且以图片扩展名结尾"}
            need_confirm = bool(mconfirm.get("needs_confirm"))
            out = {
                "ok": True,
                "thread_id": tid,
                "status": "need_confirm" if need_confirm else "ready",
                "mother_card": mother_card,
                "rendered_stem": rendered_stem,
                "reply": reply[:400],
            }
            if need_confirm:
                anchor = mother_card.get("anchor") or {}
                out["kg_candidates"] = [{
                    "chapter_id": anchor.get("chapter_id"),
                    "chapter_name": anchor.get("chapter_name"),
                    "grade_book_name": anchor.get("grade_book_name"),
                    "confidence": anchor.get("confidence"),
                }]
                out["confirm_flags"] = mconfirm.get("flags") or []
            return out
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 2. 确认章轮 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def confirm_variant_chapter(thread_id: str, chapter_id: str) -> dict:
        """确认母题所属章节（need_confirm 分支续跑）→ 回填锚点重锚（~3s）。

        参数: thread_id（make_variants 返回）；chapter_id（从 kg_candidates 挑的真实章节 id，字符串）。
        返回: {ok, thread_id, status:"ready", mother_card}。之后调 generate_variants。
        """
        try:
            resp = await tk.invoke(
                "确认母题章节", thread_id,
                agent_config={"confirmed_chapter_id": str(chapter_id)},
            )
            reply = _content_text(resp)
            art = await _artifact(thread_id)
            mother_card = ((art or {}).get("header") or {}).get("mother_card")
            return {"ok": True, "thread_id": thread_id, "status": "ready",
                    "mother_card": mother_card, "reply": reply[:400]}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 3. 生成变式 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def generate_variants(thread_id: str, auto_verify: bool = False) -> dict:
        """触发变式生成（start_variants 续跑，LLM 轮 ~70s）→ 取题组。

        参数:
          thread_id  : make_variants/confirm 后的会话 id。
          auto_verify: True=生成后每题自动 sympy 验算；False（默认，产品口径）=每题 tier=pending，
                       按需 verify_variant。透传 agent_config.auto_verify。
        返回: {ok, thread_id, count, variants:[{item_id, seq, stem, answer, solution, qtype,
              difficulty, tier, verify_status, figure_spec, dna, question_id}]}。
        🔴 生成偶发首轮空题组（opus 对压轴母题首轮不收尾）——本工具按 AC4「重试≤2」自动再触发一次
           start_variants（实测第二次即出题）；两次仍空 → ok:false + 引擎回文，交驱动 agent 处置。
        """
        try:
            reply = ""
            variants: list = []
            for _attempt in range(2):  # AC4：LLM 步骤重试 ≤2（首轮偶发空 → 再触发一次即出题）
                resp = await tk.invoke(
                    "开始举一反三", thread_id,
                    agent_config={"start_variants": True, "auto_verify": bool(auto_verify)},
                )
                reply = _content_text(resp)
                art = await _artifact(thread_id)
                items = (art or {}).get("items") or []
                variants = [_map_variant(it) for it in items if not it.get("_dropped")]
                if variants:
                    break
            if not variants:
                return {"ok": False, "thread_id": thread_id, "count": 0, "variants": [],
                        "error": "变式生成为空（已重试 2 次）",
                        "hint": reply[:400] or "母题可能未就绪/需确认章，核对 make_variants 的 status 或换更清晰母题"}
            return {"ok": True, "thread_id": thread_id, "count": len(variants),
                    "variants": variants, "reply": reply[:400]}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 4. 单题验算 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def verify_variant(thread_id: str, item_id: str) -> dict:
        """对某道变式独立验算（无状态 sympy 重算，~17s）→ 判决。persist 前须逐题过（说明书铁律）。

        参数: thread_id；item_id（generate_variants 返回的变式 id，= seq，字符串）。
        返回: {ok, item_id, verdict:"pass"|"fail"|"degrade", reason, computed}。
          pass=标答自洽 / fail=标答错(computed=真算值) / degrade=sympy 吃不下转人工（非判错）。
        """
        try:
            art = await _artifact(thread_id)
            it = _find_item(art, item_id)
            if it is None:
                return {"ok": False, "error": f"thread {thread_id} 无 item {item_id}",
                        "hint": "先 generate_variants 拿 item_id，或核对 thread_id"}
            body = {"stem": it.get("stem") or "", "answer": it.get("answer") or "",
                    "qtype": it.get("qtype") or None}
            resp = await tk.variant_call("/variant/verify-one", body)
            return {"ok": True, "item_id": str(item_id), "verdict": resp.get("verdict"),
                    "reason": resp.get("detail"), "computed": resp.get("computed")}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 5. 改题 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def edit_variant(thread_id: str, item_id: str, patch: dict = None) -> dict:
        """手动编辑某道变式（零 LLM，只 patch 传入字段）→ 标手动编辑态。

        参数:
          thread_id；item_id（= seq，字符串）；
          patch: {stem?, answer?, analyze?}（analyze 映射 toolkit 的 solution 字段；None 键不改）。
        返回: {ok, item_id, item}（编辑后该题快照）。改后建议重跑 verify_variant。
        """
        try:
            patch = patch or {}
            art = await _artifact(thread_id)
            it = _find_item(art, item_id)
            if it is None:
                return {"ok": False, "error": f"thread {thread_id} 无 item {item_id}",
                        "hint": "先 generate_variants 拿 item_id"}
            index = it.get("index")
            body = {"thread_id": thread_id, "index": int(index)}
            if patch.get("stem") is not None:
                body["stem"] = patch["stem"]
            if patch.get("answer") is not None:
                body["answer"] = patch["answer"]
            # 契约 patch.analyze → toolkit edit-item 的 solution（解析/详解同一载体）
            sol = patch.get("analyze", patch.get("solution"))
            if sol is not None:
                body["solution"] = sol
            resp = await tk.variant_call("/variant/edit-item", body)
            new_art = resp if isinstance(resp, dict) and resp.get("items") else (resp or {}).get("artifact") or {}
            new_it = _find_item(new_art, item_id) or _find_item(await _artifact(thread_id), item_id)
            return {"ok": True, "item_id": str(item_id),
                    "item": _map_variant(new_it) if new_it else None}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 6. 配图 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def compose_variant_figure(thread_id: str, item_id: str, dsl: dict = None) -> dict:
        """为某道变式造图（~12s）→ 中性 JSXGraph DSL（bbox+objects），客户端 GeoEngine 渲活图。

        参数:
          thread_id；item_id（= seq，字符串）；
          dsl: 传入=覆盖重绘（把该 DSL 作修正依据令引擎照此重画；不传=从题面/figure_spec 现推）。
        返回: {ok, item_id, needs_figure, figure_spec}（figure_spec=JSXGraph DSL：{bbox, objects[]}）；
          造图降级 → {ok:false, needs_figure:true, reason}。
        """
        try:
            art = await _artifact(thread_id)
            it = _find_item(art, item_id)
            if it is None:
                return {"ok": False, "error": f"thread {thread_id} 无 item {item_id}",
                        "hint": "先 generate_variants 拿 item_id"}
            body = {
                "mode": "compose_variant", "thread_id": thread_id,
                "ruoyi_token": tk.require_token(),
                "stem": it.get("stem") or "", "answer": it.get("answer") or "",
                "item_id": str(item_id), "format": "dsl",
            }
            if dsl:
                # 覆盖重绘：端点无直传 dsl 字段 → 作为修正提示令引擎照此 DSL 重画（见说明书铁律）
                import json
                body["correction_prompt"] = "请严格按以下几何 DSL 覆盖重绘：" + json.dumps(dsl, ensure_ascii=False)
            resp = await tk.variant_call("/variant/compose-figure", body)
            ok = bool(resp.get("ok"))
            return {"ok": ok, "item_id": str(item_id), "needs_figure": resp.get("needs_figure"),
                    "figure_spec": resp.get("dsl"), "reason": resp.get("reason")}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ───────────────────────── 7. 落库 ─────────────────────────
    @mcp.tool(tags={"variant"})
    async def persist_variants(thread_id: str, item_ids: list = None) -> dict:
        """把变式落库拿真实 qid（owner=登录老师）。🔴 落库前须逐题 verify_variant（说明书铁律）。

        参数:
          thread_id；item_ids: 只落指定几题（item_id/seq 字符串列表）；缺省=全部入库。
        返回: {ok, results:[{item_id, question_id:str}], view_url}（题库页深链，老师视觉验收入口）。
        """
        try:
            token = tk.require_token()
            if item_ids:
                art = await _artifact(thread_id)
                for iid in item_ids:
                    it = _find_item(art, iid)
                    if it is None:
                        continue
                    await tk.variant_call(
                        "/variant/persist-one",
                        {"thread_id": thread_id, "index": int(it.get("index")), "ruoyi_token": token},
                    )
            else:
                await tk.variant_call("/variant/persist", {"thread_id": thread_id, "ruoyi_token": token})
            # 回读 artifact 收 {item_id, question_id}
            art = await _artifact(thread_id)
            results = []
            for it in (art.get("items") or []):
                if it.get("question_id"):
                    seq = it.get("seq") if it.get("seq") is not None else it.get("index")
                    results.append({"item_id": str(seq), "question_id": str(it["question_id"])})
            return {"ok": True, "thread_id": thread_id, "results": results,
                    "view_url": "http://localhost:9091/question/index"}
        except Exception as e:  # noqa: BLE001
            return _err(e)
