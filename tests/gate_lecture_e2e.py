"""G11（讲义 备份→删→重录 闭环）：用户授权口径的不可逆操作，删前必落盘备份。

链路：login(admin=官方讲义库 owner uid1)
     → list_lecture_docs 确认目录非空 + 定位实证课时（无脊椎动物 901002003001，2026-07-07 端到端实证课时）
     → 挑一个知识点级片段（L5，15 位 subjectId：901002003001003 扁形动物）
     → 【删前保险】DB 读原始 content_json + get_lecture_content 读 text/example_qids
       → 完整 dump 到 prd/PRD-O-005/artifacts/g11-lecture-backup.json（写失败=立即中止，不删）
     → remove_lecture_frag(subject_prefix=15位, owner=1) → 断言 removed
     → get_lecture_content 断言该片段已不在；🔴 兄弟片段（901002003001002 刺胞动物）仍在（前缀没扩大化的硬证据）
     → 用备份原样 save_lecture_frag 重录 → get_lecture_content 断言恢复（text 逐字一致 + example_qids 一致 + DB 行回来）。

🔴 铁律：只用知识点级 15 位前缀删单点，绝不用课时级 12 位前缀（连思维导图一起删的已知坑）；只动这一个片段。
🔴 读侧/写侧结构差异：get_lecture_content 只回 text（Tiptap→纯文本），不回原始 contentJson；
   故重录所需的原始 contentJson 从 DB biz_kg_lecture_frag.content_json 读（删前保险的一部分），
   回读一致性用 get_lecture_content.text 逐字比对（对 JSON 序列化差异稳健，且是真内容级断言）。
"""
import json
from pathlib import Path

import pytest
from fastmcp import Client

from teacher_mcp.server import build_server
from teacher_mcp.backends import db

# 实证课时（无脊椎动物）下一个知识点级 15 位片段 + 一个兄弟片段（回验前缀未扩大化）
COURSE_L4 = "901002003001"          # 课时（12 位）——🔴 只读定位，绝不作删除前缀
TARGET_L5 = "901002003001003"       # 扁形动物（知识点 L5，15 位）——删/重录对象
SIBLING_L5 = "901002003001002"      # 刺胞动物（兄弟片段）——删后须仍在
BOOK_ID = "CC7S"
OWNER = 1                           # admin=uid1 官方讲义库

# codeplace-O/prd/PRD-O-005/artifacts/g11-lecture-backup.json
_ARTIFACTS = Path(__file__).resolve().parents[2] / "prd" / "PRD-O-005" / "artifacts"
BACKUP_FILE = _ARTIFACTS / "g11-lecture-backup.json"


def _read_frag_raw(subject_id: str):
    """DB 只读取某片段原始行（content_json 原文 + title + status）。无行返回 None。"""
    conn = db.conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, content_json, status FROM biz_kg_lecture_frag"
                " WHERE CAST(subject_id AS CHAR)=%s AND owner_id=%s AND book_id=%s",
                (subject_id, OWNER, BOOK_ID))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"title": row[0], "content_json": row[1], "status": row[2] or "0"}


@pytest.mark.asyncio
async def test_lecture_backup_remove_restore():
    async with Client(build_server("all"), timeout=180) as c:
        r = (await c.call_tool("login", {})).data
        assert r.get("ok", True), f"login 失败: {r}"

        # ── 目录非空 + 课时定位 ──
        cat = (await c.call_tool("list_lecture_docs", {"book_id": BOOK_ID})).data
        assert cat.get("ok"), f"list_lecture_docs 失败: {cat}"
        assert cat.get("lessons"), f"讲义目录为空，无实证样本可测: {cat}"

        # ── 删前保险①：get_lecture_content 读目标片段 text/example_qids ──
        before = (await c.call_tool("get_lecture_content", {
            "subject_id": TARGET_L5, "book_id": BOOK_ID, "owner": str(OWNER)})).data
        assert before.get("ok"), f"get_lecture_content(before) 失败: {before}"
        assert before.get("has_content"), f"目标片段删前无内容，样本失效: {before}"
        text_before = before.get("text") or ""
        qids_before = before.get("example_qids") or []
        assert text_before.strip(), f"目标片段 text 为空: {before}"

        # ── 删前保险②：DB 读原始 content_json（重录所需，读侧不回吐）──
        raw = _read_frag_raw(TARGET_L5)
        assert raw is not None, f"DB 未找到目标片段 {TARGET_L5}（owner={OWNER} book={BOOK_ID}）"
        content_doc = json.loads(raw["content_json"])
        assert content_doc.get("type") == "doc" and content_doc.get("content"), \
            f"原始 content_json 结构异常: {list(content_doc.keys())}"

        # ── 落盘备份（🔴 不可逆操作前的保险；写失败=立即中止不删）──
        backup = {
            "subjectId": TARGET_L5, "bookId": BOOK_ID, "owner": OWNER,
            "title": raw["title"], "status": raw["status"],
            "contentJson": content_doc,        # 原始 Tiptap doc（重录源）
            "text_before": text_before,        # 回读一致性基准
            "example_qids_before": qids_before,
        }
        _ARTIFACTS.mkdir(parents=True, exist_ok=True)
        BACKUP_FILE.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
        assert BACKUP_FILE.exists() and BACKUP_FILE.stat().st_size > 0, \
            f"备份落盘失败，中止（不执行删除）: {BACKUP_FILE}"

        # ── 兄弟片段删前基准（回验前缀未扩大化）──
        sib_before = (await c.call_tool("get_lecture_content", {
            "subject_id": SIBLING_L5, "book_id": BOOK_ID, "owner": str(OWNER)})).data
        assert sib_before.get("has_content"), f"兄弟片段删前无内容，样本失效: {sib_before}"
        sib_text_before = sib_before.get("text") or ""

        # ── 删（知识点级 15 位前缀，绝不用课时 12 位）──
        assert len(TARGET_L5) == 15, "目标必须是 15 位知识点级 subjectId"
        rm = (await c.call_tool("remove_lecture_frag", {
            "subject_prefix": TARGET_L5, "book_id": BOOK_ID, "owner": OWNER})).data
        assert rm.get("ok") is not False, f"remove_lecture_frag 失败: {rm}"
        removed = rm.get("removed")
        assert removed and int(removed) >= 1, f"removed 应≥1: {rm}"

        # ── 断言目标已不在 + 兄弟仍在（前缀没扩大化的硬证据）──
        gone = (await c.call_tool("get_lecture_content", {
            "subject_id": TARGET_L5, "book_id": BOOK_ID, "owner": str(OWNER)})).data
        assert not gone.get("has_content") and not (gone.get("text") or "").strip(), \
            f"目标片段删后仍有内容: {gone}"
        assert _read_frag_raw(TARGET_L5) is None, "DB 中目标片段删后仍在"

        sib_after = (await c.call_tool("get_lecture_content", {
            "subject_id": SIBLING_L5, "book_id": BOOK_ID, "owner": str(OWNER)})).data
        assert sib_after.get("has_content"), f"🔴 兄弟片段被误删（前缀扩大化）: {sib_after}"
        assert (sib_after.get("text") or "") == sib_text_before, \
            "兄弟片段内容删后发生变化（不该动）"

        # ── 用备份原样重录 ──
        save = (await c.call_tool("save_lecture_frag", {
            "frags": [{
                "subjectId": backup["subjectId"],
                "title": backup["title"],
                "contentJson": backup["contentJson"],
                "status": backup["status"],
            }],
            "book_id": BOOK_ID,
            "owner": OWNER,
        })).data
        assert save.get("ok") is not False, f"save_lecture_frag 重录失败: {save}"
        results = save.get("results") or []
        assert any(str(x.get("subjectId")) == TARGET_L5 for x in results if isinstance(x, dict)), \
            f"重录结果未含目标片段: {save}"

        # ── 断言恢复：text 逐字一致 + example_qids 一致 + DB 行回来 ──
        after = (await c.call_tool("get_lecture_content", {
            "subject_id": TARGET_L5, "book_id": BOOK_ID, "owner": str(OWNER)})).data
        assert after.get("ok") and after.get("has_content"), f"重录后无内容: {after}"
        assert (after.get("text") or "") == text_before, (
            f"重录后 text 与备份不一致\n--before--\n{text_before!r}\n--after--\n{after.get('text')!r}")
        assert (after.get("example_qids") or []) == qids_before, \
            f"重录后 example_qids 不一致: {after.get('example_qids')} vs {qids_before}"
        assert _read_frag_raw(TARGET_L5) is not None, "重录后 DB 中目标片段仍缺失"

        print(f"G11 PASS 删前片段有内容(text {len(text_before)}字)→removed={removed}→"
              f"目标 has_content=False 兄弟仍在→重录 text 逐字一致；备份={BACKUP_FILE}")
