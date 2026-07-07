"""G3（AC3）：数据线端到端 —— MCP 工具真录一题（带测试标记）→ 残留验证 → 回读。

打真 BE（:9090）真库（ai_lesson_prep@3307）。测试数据题干带 [PRD-O-005-TEST] 标记（O 线共库纪律，可按标记清理）。
走 in-memory Client 调 MCP 工具（非裸 HTTP——验的是工具面）。
断言：① ingest ok 且拿到 qid；② verify_ingest 残留=0；③ get_question 回读题干含标记（qid 真实落库的硬证据）。
"""
import pytest
from fastmcp import Client

from teacher_mcp.server import build_server

STEM = ("[PRD-O-005-TEST] （2024·测试卷）计算：$3x-5=7$ 时 $x$ 的值为（  ）")


@pytest.mark.asyncio
async def test_ingest_verify_readback():
    async with Client(build_server("all")) as c:
        r = await c.call_tool("login", {})
        assert r.data.get("ok", True), f"login 失败: {r.data}"

        r = await c.call_tool("ingest_items", {
            "items": [{
                "stem": STEM,
                "options": ["$x=4$", "$x=2$", "$x=-4$", "$x=12$"],
                "answer": "A",
                "analyze": "移项得 $3x=12$，$x=4$。[PRD-O-005-TEST]",
                "question_type": 1,
            }],
            "subject_root": "100",
        })
        data = r.data
        assert data.get("ok"), f"ingest_items 失败: {data}"
        results = data.get("results") or []
        assert results and results[0].get("question_id"), f"未拿到 qid: {data}"
        qid = str(results[0]["question_id"])
        # 断言剥前缀确实发生（题干带（2024·测试卷）前缀，应进 warnings/source_raw 而非留在题干）
        r = await c.call_tool("verify_ingest", {"question_ids": [qid]})
        assert r.data.get("ok"), f"verify_ingest 失败: {r.data}"
        assert r.data.get("residue_count") == 0, f"来源前缀残留≠0: {r.data}"

        r = await c.call_tool("get_question", {"ids": [qid]})
        assert r.data.get("ok", True), f"get_question 失败: {r.data}"
        text = str(r.data)
        assert "PRD-O-005-TEST" in text, f"回读题干不含测试标记: {str(r.data)[:300]}"
