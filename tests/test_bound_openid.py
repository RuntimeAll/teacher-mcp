"""PRD-007 CLI 驱动版·后端锁身份（BOUND_OPENID）单测（mock 请求层，不打真 BE）。

场景：飞书 bot 驱动 headless claude CLI，CLI spawn 本 MCP（stdio）。为防提示注入（用户消息里写
「用 admin 身份」），身份不由模型调 login_as 决定，而由后端设的 env BOUND_OPENID 锁死。

覆盖三条契约：
  ① bound_openid 非空 → build_server 工具列表里**没有** login / login_as（模型无法自行切身份）。
  ② bound_openid 非空 + mock 请求层 → 首次调一个 teacher 工具（list_kg_tree）自动触发 /auth/botLogin
     （用 bound_openid），**绝不**走 /auth/login 用户名密码。
  ③ bound_openid 为空 → login / login_as 仍在工具列表（现有行为完全不变）。
"""
import pytest
from fastmcp import Client, FastMCP

from teacher_mcp.config import settings
from teacher_mcp.backends.ruoyi import RuoyiClient
from teacher_mcp.server import build_server
from teacher_mcp.tools import shared as tool_shared


class FakeResp:
    """最小 httpx.Response 替身：只需 status_code / .json() / .text。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


async def _names(role: str) -> set:
    async with Client(build_server(role)) as c:
        return {t.name for t in await c.list_tools()}


@pytest.mark.asyncio
async def test_bound_openid_hides_auth_tools(monkeypatch):
    """① bound_openid 非空 → 工具列表无 login / login_as（其余共享工具照常在，仅少这两个）。"""
    monkeypatch.setattr(settings, "bound_openid", "ou_bound")
    names = await _names("all")
    assert "login" not in names, "bound_openid 非空时 login 应被隐藏"
    assert "login_as" not in names, "bound_openid 非空时 login_as 应被隐藏"
    # 业务/读侧共享工具不受影响，照常暴露
    assert {"list_kg_tree", "search_questions", "get_question", "health_check"} <= names


@pytest.mark.asyncio
async def test_empty_bound_openid_keeps_auth_tools(monkeypatch):
    """③ bound_openid 为空（默认）→ login / login_as 仍在工具列表（SDK/交互路径现有行为不变）。"""
    monkeypatch.setattr(settings, "bound_openid", "")
    names = await _names("all")
    assert "login" in names
    assert "login_as" in names


def _mini_server(client: RuoyiClient) -> FastMCP:
    """只挂 shared 组的 FastMCP，注入可控 client（不碰真底座）；hide_auth_tools=True 模拟锁身份态。"""
    mcp = FastMCP("test-bound-openid")
    tool_shared.register(mcp, client, "data", hide_auth_tools=True)
    return mcp


@pytest.mark.asyncio
async def test_first_teacher_tool_auto_botlogin(monkeypatch):
    """② bound_openid 非空 + 首次调 teacher 工具（list_kg_tree）→ 自动 /auth/botLogin（用 bound_openid），
    不走 /auth/login 用户名密码；会话切到 bound_openid 身份。"""
    monkeypatch.setattr(settings, "bound_openid", "ou_locked")
    monkeypatch.setattr(settings, "bot_secret", "test-secret")
    client = RuoyiClient()

    post_calls = []

    async def fake_post(path, json=None, headers=None):
        post_calls.append({"path": path, "json": json, "headers": headers})
        # botLogin 成功签发（若被误当密码登录调 /auth/login，此断言在下方兜住）
        return FakeResp(200, {"code": 200, "data": {"access_token": "tok-bound", "user_id": 99}})

    async def fake_request(method, path, **kwargs):
        # 建会话后 teacher_call 的真实请求 → 返回 envelope code==1
        return FakeResp(200, {"code": 1, "response": [{"id": "1", "name": "n"}]})

    monkeypatch.setattr(client._client, "post", fake_post)
    monkeypatch.setattr(client._client, "request", fake_request)
    try:
        async with Client(_mini_server(client)) as c:
            r = await c.call_tool("list_kg_tree", {})
        assert r.data.get("ok") is True, r.data
        # 恰好触发一次 botLogin，且用 bound_openid + 服务密钥头
        assert len(post_calls) == 1, post_calls
        assert post_calls[0]["path"] == "/auth/botLogin"
        assert post_calls[0]["json"]["openid"] == "ou_locked"
        assert post_calls[0]["headers"]["X-Bot-Secret"] == "test-secret"
        # 🔴 绝不走用户名密码登录
        assert all(pc["path"] != "/auth/login" for pc in post_calls)
        # 会话已切到 bound_openid 身份
        assert client.current_openid == "ou_locked"
        assert client.user_id == 99
        # 免密身份不留用户名密码
        assert client.username is None
    finally:
        await client.aclose()
