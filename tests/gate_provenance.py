"""G12（PRD-O-005 溯源增强）：双管道来源标记 + 快速找回，端到端硬证据。

打真 BE（:9090）真库（ai_lesson_prep@3307），走 in-memory Client 调 MCP 工具（验工具面）。
测试题干带 [PRD-O-005-TEST] 标记 + 毫秒时戳（保证每次是全新插入 → create_time 落当下、不撞 dedup）。
断言链：
  ① ingest_items 返回 batch_id 非空；pymysql 查回该 qid → import_source=='mcp-all'（!= 'main'，双管道硬证据）
     且 import_batch_id==返回 batch_id（BE IngestServiceImpl 直落，非 db fallback）。
  ② search_questions(batch_id=) 恰好命中该题；search_questions(since='1h', mine=True) 包含该题。
  ③ my_recent_uploads(hours=1) → questions 含该 qid、按批次分组正确。
"""
import time

import pymysql
import pytest
from fastmcp import Client

from teacher_mcp.config import settings
from teacher_mcp.server import build_server

_UNIQ = str(int(time.time() * 1000))
STEM = f"[PRD-O-005-TEST] 溯源批次测试 {_UNIQ}：计算 $12+34$ 的值。"


def _db():
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_database, charset="utf8mb4")


@pytest.mark.asyncio
async def test_provenance_dual_pipeline_and_recall():
    async with Client(build_server("all")) as c:  # all → 角色 mcp-all
        r = await c.call_tool("login", {})
        assert r.data.get("ok", True), f"login 失败: {r.data}"

        # ── 录 1 题（单管道机录）──
        r = await c.call_tool("ingest_items", {
            "items": [{"stem": STEM, "answer": "46", "analyze": f"12+34=46。{_UNIQ}", "question_type": 7}],
            "subject_root": "100",
        })
        data = r.data
        assert data.get("ok"), f"ingest_items 失败: {data}"
        batch_id = data.get("batch_id")
        assert batch_id, f"返回无 batch_id: {data}"
        assert batch_id.startswith("mcp-"), f"batch_id 格式异常: {batch_id}"
        results = data.get("results") or []
        assert results and results[0].get("question_id"), f"未拿到 qid: {data}"
        qid = str(results[0]["question_id"])

        # ── ① 双管道硬证据：pymysql 查回 import_source / import_batch_id ──
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT import_source, import_batch_id FROM biz_question WHERE id=%s", (int(qid),))
                row = cur.fetchone()
        finally:
            conn.close()
        assert row, f"pymysql 查无此题 qid={qid}"
        assert row[0] == "mcp-all", f"import_source={row[0]!r} 期望 'mcp-all'"
        assert row[0] != "main", "import_source 仍是 main（与手工不可区分）"
        assert row[1] == batch_id, f"import_batch_id={row[1]!r} != 返回 batch_id {batch_id!r}"

        # ── ② search_questions 找回路径 ──
        r = await c.call_tool("search_questions", {"batch_id": batch_id})
        assert r.data.get("ok"), f"batch 检索失败: {r.data}"
        ids = [it["id"] for it in r.data.get("items", [])]
        assert ids == [qid], f"batch 检索期望恰好 [{qid}]，实得 {ids}"

        r = await c.call_tool("search_questions", {"since": "1h", "mine": True})
        assert r.data.get("ok"), f"since 检索失败: {r.data}"
        ids2 = [it["id"] for it in r.data.get("items", [])]
        assert qid in ids2, f"since=1h,mine 未含 {qid}: {ids2[:10]}"

        # ── ③ my_recent_uploads 分组找回 ──
        r = await c.call_tool("my_recent_uploads", {"hours": 1})
        d = r.data
        assert d.get("ok"), f"my_recent_uploads 失败: {d}"
        grp = None
        for b in d["questions"]["batches"]:
            if b.get("batch_id") == batch_id:
                grp = b
                break
        assert grp, f"my_recent_uploads 无批次 {batch_id}: {[b.get('batch_id') for b in d['questions']['batches']][:10]}"
        assert grp["import_source"] == "mcp-all", f"批次 import_source={grp['import_source']!r}"
        assert any(it["id"] == qid for it in grp["items"]), f"批次 {batch_id} 内无 qid {qid}"
