"""G 系冒烟：各 ROLE 视图工具名集合断言（in-memory Client，不碰库）。

兼容基线硬编码进本测试：旧 34 工具名单（从旧 server.py 的 _SHARED/_GROUPS 推出）+ health_check。
tag 过滤口径见 server.ROLE_TAGS；每 ROLE 视图 = 旧对应组 ∪ {health_check}。
"""
import pytest
from fastmcp import Client

from teacher_mcp.server import build_server

# ── 兼容基线（旧 34 名单，按 shared/group 分块）──
SHARED = {"login", "list_kg_tree", "resolve_kg", "search_questions", "get_question", "get_role_manual"}
HEALTH = {"health_check"}

# 非共享·录入组（旧 ingest 组新增 9）
INGEST_ONLY = {"format_question", "upload_image", "ingest_question", "ingest_items", "verify_ingest",
               "convert_doc", "convert_pdf", "parse_paper_text", "label_question"}
# 非共享·讲义组（旧 lecture 组新增 8，与 ingest 组在 convert_* 三个上重叠）
LECTURE_ONLY = {"convert_lecture_docx", "save_lecture_frag", "remove_lecture_frag",
                "list_lecture_docs", "get_lecture_content", "convert_doc", "convert_pdf", "parse_paper_text"}
# 非共享·备课组（旧 prep 组新增 18）
PREP_ONLY = {"create_teach_target", "list_teach_targets", "upsert_course_plan", "schedule_sessions",
             "list_schedule", "update_session", "build_prep_pack", "render_prep_pack", "submit_review",
             "get_student_profile", "get_plan_detail", "list_lecture_docs", "get_lecture_content",
             "compose_paper", "create_paper", "update_paper", "ingest_items", "upload_image"}

ALL_34 = SHARED | INGEST_ONLY | LECTURE_ONLY | PREP_ONLY


async def _names(role: str) -> set:
    async with Client(build_server(role)) as c:
        return {t.name for t in await c.list_tools()}


def test_baseline_is_34():
    assert len(ALL_34) == 34, sorted(ALL_34)


@pytest.mark.asyncio
async def test_role_all():
    assert await _names("all") == ALL_34 | HEALTH  # 35


@pytest.mark.asyncio
async def test_role_ingest():
    assert await _names("ingest") == SHARED | INGEST_ONLY | HEALTH  # 16 = 旧15 ∪ health


@pytest.mark.asyncio
async def test_role_lecture():
    assert await _names("lecture") == SHARED | LECTURE_ONLY | HEALTH  # 15 = 旧14 ∪ health


@pytest.mark.asyncio
async def test_role_prep():
    assert await _names("prep") == SHARED | PREP_ONLY | HEALTH  # 25 = 旧24 ∪ health


@pytest.mark.asyncio
async def test_role_data():
    # data == ingest ∪ lecture ∪ {health_check}
    assert await _names("data") == SHARED | INGEST_ONLY | LECTURE_ONLY | HEALTH  # 21


@pytest.mark.asyncio
async def test_role_variant():
    # 本批 variant 组还没有工具，空组 → 只剩 shared ∪ health
    assert await _names("variant") == SHARED | HEALTH  # 7


@pytest.mark.asyncio
async def test_get_role_manual_callable():
    """挑一个工具 in-memory 真调（不碰库）：get_role_manual 返回 ok。"""
    async with Client(build_server("all")) as c:
        r = await c.call_tool("get_role_manual", {"role": "data"})
    assert r.data.get("ok") is True
    assert "录入角色说明书" in r.data.get("manual", "")
