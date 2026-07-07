"""G4（AC4）：举一反三全链路 gate —— 走 MCP 工具（非裸 HTTP），含真落库。

链路：login → make_variants(带图母题) → [need_confirm 则 confirm] → generate_variants
     → verify_variant → edit_variant(打 [PRD-O-005-TEST] 标记，测试数据纪律)
     → compose_variant_figure(断言 bbox+objects) → persist_variants → qid 回读。
依赖：toolkit :9093 + BE :9090 在跑。LLM 轮 60-120s/轮属正常，全链 ~4 分钟。
🔴 AC4 口径「实测跑通不卡阈值」：generate 内部已有 ≤2 重试；本 gate 不再外层重试。
"""
import pytest
from fastmcp import Client

from teacher_mcp.server import build_server

IMAGE_URL = "https://ai-book.oss-cn-hangzhou.aliyuncs.com/2026/06/30/7a78827e1fd046d2b87d95f54cebaaf8.png"
MARK = "[PRD-O-005-TEST]"


@pytest.mark.asyncio
async def test_variant_full_chain_with_persist():
    async with Client(build_server("variant"), timeout=900) as c:
        r = (await c.call_tool("login", {})).data
        assert r.get("ok", True), f"login: {r}"

        m = (await c.call_tool("make_variants", {"image_url": IMAGE_URL, "count": 3})).data
        assert m.get("ok"), f"make_variants: {m}"
        thread_id = m["thread_id"]

        if m.get("status") == "need_confirm":
            cands = [k for k in (m.get("kg_candidates") or []) if k.get("chapter_id")]
            assert cands, f"need_confirm 但无可用 chapter_id 候选（驱动侧需 resolve_kg，gate 环境不该走到这）: {m}"
            cf = (await c.call_tool("confirm_variant_chapter",
                                    {"thread_id": thread_id, "chapter_id": str(cands[0]["chapter_id"])})).data
            assert cf.get("ok") and cf.get("status") == "ready", f"confirm: {cf}"

        g = (await c.call_tool("generate_variants", {"thread_id": thread_id})).data
        assert g.get("ok"), f"generate_variants: {g}"
        variants = g.get("variants") or []
        assert len(variants) >= 1, f"变式数 <1: {g}"
        item_id = str(variants[0]["item_id"])

        v = (await c.call_tool("verify_variant", {"thread_id": thread_id, "item_id": item_id})).data
        assert v.get("ok") and v.get("verdict") == "pass", f"verify_variant: {v}"

        stem0 = variants[0]["stem"]
        e = (await c.call_tool("edit_variant", {
            "thread_id": thread_id, "item_id": item_id,
            "patch": {"stem": f"{MARK} {stem0}"},
        })).data
        assert e.get("ok"), f"edit_variant: {e}"

        f = (await c.call_tool("compose_variant_figure",
                               {"thread_id": thread_id, "item_id": item_id})).data
        assert f.get("ok"), f"compose_variant_figure: {f}"
        spec = f.get("figure_spec") or {}
        assert spec.get("bbox") and spec.get("objects"), f"DSL 缺 bbox/objects: {list(spec.keys())}"

        p = (await c.call_tool("persist_variants",
                               {"thread_id": thread_id, "item_ids": [item_id]})).data
        assert p.get("ok"), f"persist_variants: {p}"
        results = p.get("results") or []
        assert results and results[0].get("question_id"), f"persist 未返 qid: {p}"
        qid = str(results[0]["question_id"])

        q = (await c.call_tool("get_question", {"ids": [qid]})).data
        assert MARK in str(q), f"落库回读不含标记(qid={qid}): {str(q)[:300]}"
