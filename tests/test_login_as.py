"""PRD-007 login_as 免密切身份单测（mock 请求层，不打真 BE）。

覆盖四条确定性路径：
  1. 成功切换      → ok:true + user_id/openid，会话态 token/openid/user_id 全换；X-Bot-Secret 头带上
  2. 403 密钥错     → ok:false + hint 含 403/forbidden，会话不被污染
  3. 未绑定 openid  → ok:false + 🔴 hint 必含「未绑定」（bot 按此字面路由拒绝话术）
  4. BOT_SECRET 缺  → ok:false + hint 提示配置缺失（不发请求）
  5. 401 自动重签   → 免密态 401 → 重调 botLogin 用当前 openid 重签而非用户名密码
"""
import pytest
from fastmcp import Client
from fastmcp import FastMCP

from teacher_mcp.config import settings
from teacher_mcp.backends.ruoyi import RuoyiClient
from teacher_mcp.tools import shared as tool_shared


class FakeResp:
    """最小 httpx.Response 替身：只需 status_code / .json() / .text。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _mini_server(client: RuoyiClient) -> FastMCP:
    """只挂 shared 组（含 login_as）的 FastMCP，注入可控 client（不碰真底座）。"""
    mcp = FastMCP("test-login-as")
    tool_shared.register(mcp, client, "data")
    return mcp


@pytest.mark.asyncio
async def test_login_as_success(monkeypatch):
    """成功切换：ok:true + 会话 token/openid/user_id 全换；密钥头 + clientId body 带上。"""
    monkeypatch.setattr(settings, "bot_secret", "test-secret")
    client = RuoyiClient()
    seen = {}

    async def fake_post(path, json=None, headers=None):
        seen["path"] = path
        seen["headers"] = headers
        seen["json"] = json
        return FakeResp(200, {"code": 200, "data": {"access_token": "tok-abc", "user_id": 42}})

    monkeypatch.setattr(client._client, "post", fake_post)
    try:
        async with Client(_mini_server(client)) as c:
            r = await c.call_tool("login_as", {"openid": "ou_test"})
        assert r.data.get("ok") is True
        assert r.data.get("user_id") == 42
        assert r.data.get("openid") == "ou_test"
        # 会话态真切换
        assert client.token == "tok-abc"
        assert client.current_openid == "ou_test"
        assert client.user_id == 42
        # 服务密钥头 + clientId body
        assert seen["path"] == "/auth/botLogin"
        assert seen["headers"]["X-Bot-Secret"] == "test-secret"
        assert seen["json"]["openid"] == "ou_test"
        assert seen["json"]["clientId"] == settings.ruoyi_client_id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_login_as_forbidden(monkeypatch):
    """403 密钥错：ok:false + hint 含 403/forbidden；会话不被污染（token 仍 None）。"""
    monkeypatch.setattr(settings, "bot_secret", "wrong-secret")
    client = RuoyiClient()

    async def fake_post(path, json=None, headers=None):
        return FakeResp(200, {"code": 403, "msg": "botLogin forbidden"})

    monkeypatch.setattr(client._client, "post", fake_post)
    try:
        async with Client(_mini_server(client)) as c:
            r = await c.call_tool("login_as", {"openid": "ou_x"})
        assert r.data.get("ok") is False
        hint = r.data.get("hint") or ""
        assert "403" in hint or "forbidden" in hint
        assert client.token is None
        assert client.current_openid is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_login_as_unbound(monkeypatch):
    """未绑定：🔴 hint 必含「未绑定」（bot 按此字面拒绝话术）。"""
    monkeypatch.setattr(settings, "bot_secret", "test-secret")
    client = RuoyiClient()

    async def fake_post(path, json=None, headers=None):
        return FakeResp(200, {"code": 500, "msg": "openid 未绑定 teacher 账号，请联系管理员"})

    monkeypatch.setattr(client._client, "post", fake_post)
    try:
        async with Client(_mini_server(client)) as c:
            r = await c.call_tool("login_as", {"openid": "ou_unbound"})
        assert r.data.get("ok") is False
        assert "未绑定" in (r.data.get("hint") or "")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_login_as_no_secret(monkeypatch):
    """BOT_SECRET 未配置：ok:false + hint 提示配置缺失（早失败，不发请求）。"""
    monkeypatch.setattr(settings, "bot_secret", "")
    client = RuoyiClient()

    async def boom(*a, **k):  # 断言不发请求
        raise AssertionError("BOT_SECRET 缺失时不应发起 botLogin 请求")

    monkeypatch.setattr(client._client, "post", boom)
    try:
        async with Client(_mini_server(client)) as c:
            r = await c.call_tool("login_as", {"openid": "ou_x"})
        assert r.data.get("ok") is False
        assert "BOT_SECRET" in (r.data.get("hint") or "")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_401_auto_resign_via_openid(monkeypatch):
    """免密态 401 → 用当前 openid 重调 botLogin 重签（非用户名密码），重放成功。"""
    monkeypatch.setattr(settings, "bot_secret", "test-secret")
    client = RuoyiClient()

    post_calls = {"n": 0}

    async def fake_post(path, json=None, headers=None):
        post_calls["n"] += 1
        return FakeResp(200, {"code": 200, "data": {"access_token": f"tok-{post_calls['n']}", "user_id": 7}})

    monkeypatch.setattr(client._client, "post", fake_post)

    # 先免密登录 → 进入 openid 态（botLogin 第 1 次）
    await client.login_as("ou_resign")
    assert client.current_openid == "ou_resign"
    assert client.username is None and client.token == "tok-1"
    assert post_calls["n"] == 1

    req_calls = {"n": 0}

    async def fake_request(method, path, **kwargs):
        req_calls["n"] += 1
        if req_calls["n"] == 1:
            return FakeResp(401, {})          # 首次 401 触发重签
        return FakeResp(200, {"code": 1, "response": {"hello": "world"}})

    monkeypatch.setattr(client._client, "request", fake_request)

    resp = await client.teacher_post("/teacher/foo", {"a": 1})
    assert resp == {"hello": "world"}          # 重放成功
    assert post_calls["n"] == 2                # 触发一次 openid 重签（botLogin 第 2 次）
    assert client.token == "tok-2"             # 重签后换新 token
    assert client.current_openid == "ou_resign"
    await client.aclose()
