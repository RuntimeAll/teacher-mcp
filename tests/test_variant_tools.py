"""举一反三工具组单测（不打真 toolkit）——PRD-O-005 批3 / G6① 地基。

两条不依赖 LLM 的确定性路径：
  1. toolkit 未起（base_url 指死端口 127.0.0.1:1 必 ConnectError）→ make_variants 返 ok:false + hint 含 9093。
  2. 未登录（无 login）→ 拿不到 ruoyi_token → ok:false + hint 含 login。
另加 variant 视图工具名集合断言（== shared6 + health + 7 variant）。
"""
import pytest
from fastmcp import Client

from teacher_mcp.backends.ruoyi import RuoyiClient
from teacher_mcp.backends.toolkit import ToolkitClient
from teacher_mcp.server import build_server
from teacher_mcp.tools import variant as tool_variant

SHARED = {"login", "list_kg_tree", "resolve_kg", "search_questions", "get_question", "get_role_manual"}
HEALTH = {"health_check"}
VARIANT_ONLY = {"make_variants", "confirm_variant_chapter", "generate_variants",
                "verify_variant", "edit_variant", "compose_variant_figure", "persist_variants"}


@pytest.mark.asyncio
async def test_variant_view_toolset():
    """variant 角色视图 == shared6 ∪ health ∪ 7 variant（= 14）。"""
    async with Client(build_server("variant")) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == SHARED | HEALTH | VARIANT_ONLY


def _mini_server(toolkit_client):
    """建一个只挂 variant 组的 FastMCP，注入可控 ruoyi/toolkit（不碰真底座）。"""
    from fastmcp import FastMCP
    mcp = FastMCP("test-variant")
    tool_variant.register(mcp, toolkit_client._ruoyi, toolkit_client)
    return mcp


@pytest.mark.asyncio
async def test_make_variants_toolkit_down():
    """toolkit 未起（假端口）→ ok:false 且 hint 含 9093（G6①）。已 login（token 存在）以越过身份闸。"""
    ruoyi = RuoyiClient()
    ruoyi._token = "fake-jwt-for-test"  # 装作已登录，让失败点落在 toolkit 连接而非身份闸
    tk = ToolkitClient(ruoyi, base_url="http://127.0.0.1:1")  # 必 ConnectError
    try:
        async with Client(_mini_server(tk)) as c:
            r = await c.call_tool(
                "make_variants",
                {"image_url": "https://ai-book.oss-cn-hangzhou.aliyuncs.com/x.png"},
            )
        assert r.data.get("ok") is False
        assert "9093" in (r.data.get("hint") or "")
    finally:
        await tk.aclose()
        await ruoyi.aclose()


@pytest.mark.asyncio
async def test_make_variants_not_logged_in():
    """未 login（无 token）→ ok:false 且 hint 含 login（身份闸早失败，不碰 toolkit）。"""
    ruoyi = RuoyiClient()  # 未登录，token=None
    tk = ToolkitClient(ruoyi, base_url="http://127.0.0.1:1")
    try:
        async with Client(_mini_server(tk)) as c:
            r = await c.call_tool(
                "make_variants",
                {"image_url": "https://ai-book.oss-cn-hangzhou.aliyuncs.com/x.png"},
            )
        assert r.data.get("ok") is False
        assert "login" in (r.data.get("hint") or "")
    finally:
        await tk.aclose()
        await ruoyi.aclose()


@pytest.mark.asyncio
async def test_verify_variant_toolkit_down():
    """确定性端点路径同样软失败：verify_variant 在 toolkit 未起时 ok:false + hint 含 9093。"""
    ruoyi = RuoyiClient()
    ruoyi._token = "fake-jwt-for-test"
    tk = ToolkitClient(ruoyi, base_url="http://127.0.0.1:1")
    try:
        async with Client(_mini_server(tk)) as c:
            r = await c.call_tool("verify_variant", {"thread_id": "t", "item_id": "1"})
        assert r.data.get("ok") is False
        assert "9093" in (r.data.get("hint") or "")
    finally:
        await tk.aclose()
        await ruoyi.aclose()
