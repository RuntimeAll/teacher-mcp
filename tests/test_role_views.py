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

# 非共享·举一反三组（PRD-O-005 批3 新增 7，契约 v2）
VARIANT_ONLY = {"make_variants", "confirm_variant_chapter", "generate_variants",
                "verify_variant", "edit_variant", "compose_variant_figure", "persist_variants"}

ALL_34 = SHARED | INGEST_ONLY | LECTURE_ONLY | PREP_ONLY


async def _names(role: str) -> set:
    async with Client(build_server(role)) as c:
        return {t.name for t in await c.list_tools()}


def test_baseline_is_34():
    assert len(ALL_34) == 34, sorted(ALL_34)


@pytest.mark.asyncio
async def test_role_all():
    # 批3 起 all 视图纳入举一反三 7 工具：34 ∪ health ∪ variant7 = 42（仍 ⊇ 旧 34，G1 兼容）
    names = await _names("all")
    assert ALL_34 <= names  # G1：⊇ 旧 34
    assert names == ALL_34 | HEALTH | VARIANT_ONLY  # 42


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
    # 批3 落地：variant 视图 = shared6 ∪ health ∪ 7 个举一反三工具 = 14
    assert await _names("variant") == SHARED | HEALTH | VARIANT_ONLY  # 14


@pytest.mark.asyncio
async def test_get_role_manual_callable():
    """挑一个工具 in-memory 真调（不碰库）：get_role_manual 返回 ok。"""
    async with Client(build_server("all")) as c:
        r = await c.call_tool("get_role_manual", {"role": "data"})
    assert r.data.get("ok") is True
    assert "录入角色说明书" in r.data.get("manual", "")


@pytest.mark.asyncio
async def test_get_role_manual_all():
    """role='all' → 总手册（PRD-O-005 收尾：手册烤进 MCP）。"""
    async with Client(build_server("all")) as c:
        r = await c.call_tool("get_role_manual", {"role": "all"})
    assert r.data.get("ok") is True
    assert r.data.get("role") == "all"
    assert "总手册" in r.data.get("manual", "")


def test_server_has_instructions():
    """FastMCP 实例带 instructions（进 MCP initialize 响应，给客户端上手指引）。"""
    srv = build_server("all")
    assert getattr(srv, "instructions", "")
    assert "get_role_manual" in srv.instructions
