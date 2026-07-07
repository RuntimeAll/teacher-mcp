"""G9（录试卷 E2E）：MCP 工具真录 2 题 + 成卷 → 回验 biz_paper_question。

链路：login → ingest_items(2 题带标记 + PaperSpec 成卷) → 断言 paper_id + stats.ok==2
     → DB 只读查 biz_paper_question：该卷题数==2 且题 id 与 results 的 qid 一致。
打真 BE（:9090）真库（ai_lesson_prep@3307）。测试数据题干/卷名带 [PRD-O-005-TEST]（O 线共库纪律，可按标记清理）。
🔴 口径：MCP ingest_items 内部 status='1'（已发布/我的库），成卷不依赖 set-public（公共池可见是另一回事）；
   本 gate 走 DB biz_paper_question 硬验卷内题数，不看前端公共可见性。
category_id = 真实卷库目录节点（biz_paper_category '3001004001'=七上单元测试），成卷落该目录。
"""
import pytest
from fastmcp import Client

from teacher_mcp.server import build_server
from teacher_mcp.backends import db

MARK = "[PRD-O-005-TEST]"
# 真实卷库目录节点 id（biz_paper_category，DB 实查存在）——成卷 subject_id 落此
CATEGORY_ID = "3001004001"


@pytest.mark.asyncio
async def test_paper_ingest_compose_readback():
    async with Client(build_server("all")) as c:
        r = (await c.call_tool("login", {})).data
        assert r.get("ok", True), f"login 失败: {r}"

        r = (await c.call_tool("ingest_items", {
            "items": [
                {
                    "stem": f"{MARK} 计算：$2x+3=9$ 时 $x$ 的值为（  ）",
                    "options": ["$x=3$", "$x=2$", "$x=6$", "$x=-3$"],
                    "answer": "A",
                    "analyze": f"移项得 $2x=6$，$x=3$。{MARK}",
                    "question_type": 1,
                },
                {
                    "stem": f"{MARK} 计算：$-3+7$ 的结果是（  ）",
                    "options": ["$4$", "$-4$", "$10$", "$-10$"],
                    "answer": "A",
                    "analyze": f"异号相加取绝对值大者符号：$7-3=4$。{MARK}",
                    "question_type": 1,
                },
            ],
            "subject_root": "100",
            "paper": {
                "name": f"{MARK} 录卷冒烟卷",
                "category_id": CATEGORY_ID,
                "total_score": 100,
                "suggest_time": 40,
            },
        })).data

        assert r.get("ok"), f"ingest_items 失败: {r}"
        stats = r.get("stats") or {}
        assert stats.get("ok") == 2, f"stats.ok≠2: {r}"
        assert stats.get("fail") == 0, f"有 fail 题: {r}"
        paper_id = r.get("paper_id")
        assert paper_id, f"未拿到 paper_id: {r}"

        # results 的 qid（保序）
        results = r.get("results") or []
        result_qids = {str(x["question_id"]) for x in results if x.get("question_id")}
        assert len(result_qids) == 2, f"results 未含 2 个 qid: {results}"

        # ── DB 只读回验：biz_paper_question 该卷题数==2 且题 id 与 results 一致 ──
        conn = db.conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT CAST(question_id AS CHAR) FROM biz_paper_question WHERE paper_id=%s",
                    (int(paper_id),))
                paper_qids = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

        assert len(paper_qids) == 2, f"卷内题数≠2（paper_id={paper_id}）: {paper_qids}"
        assert paper_qids == result_qids, (
            f"卷内题 id 与 results qid 不一致: 卷={paper_qids} results={result_qids}")

        print(f"G9 PASS paper_id={paper_id} 卷内题={sorted(paper_qids)} stats={stats}")
