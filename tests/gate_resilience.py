"""G6②（AC5）：401 自动重登 —— token 失效后自动重登一次并重放，拿到真数据。

🔴 实测（2026-07-07）：/teacher/** 在 BE 上完全无鉴权（不带任何 header 也 200 返数据），
真 BE 永远打不出 401 → 重登逻辑在真环境不可触发。故本 gate 分两层：
  ① 真 BE 基线：登录态 lazy_tree 返回非空（功能本身不坏）；
  ② MockTransport 确定性层：首个 /teacher/** 调用强制 401 → 断言 client 自动走
     /auth/login → /system/user/getInfo → 重放原请求成功，且全程只重登一次。
被测对象 = RuoyiClient 的重试逻辑（客户端行为），mock 是正确的测试层级。
"""
import json

import httpx
import pytest

from teacher_mcp.backends.ruoyi import RuoyiClient
from teacher_mcp.config import settings


@pytest.mark.asyncio
async def test_real_be_baseline():
    assert settings.ruoyi_username, "需 .env 配 RUOYI_USERNAME/PASSWORD（dev admin）"
    c = RuoyiClient()
    try:
        await c.login(settings.ruoyi_username, settings.ruoyi_password)
        assert await c.lazy_tree({}), "基线：登录态 lazy_tree 应返回非空树"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_auto_relogin_on_401_mock():
    calls = {"login": 0, "teacher": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/auth/login":
            calls["login"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"access_token": f"tok{calls['login']}"}})
        if path == "/system/user/getInfo":
            return httpx.Response(200, json={"code": 200, "data": {"user": {"userId": 1}}})
        calls["teacher"] += 1
        auth = req.headers.get("Authorization", "")
        if auth == "Bearer tok2":  # 只认重登后的新 token
            return httpx.Response(200, json={"code": 1, "message": "ok", "response": {"hit": True}})
        return httpx.Response(401)

    c = RuoyiClient()
    await c._client.aclose()
    c._client = httpx.AsyncClient(base_url="http://mock", transport=httpx.MockTransport(handler))
    try:
        await c.login("u", "p")           # 拿到 tok1
        assert calls["login"] == 1
        data = await c.teacher_post("/teacher/anything", {})   # tok1 → 401 → 重登 tok2 → 重放成功
        assert data == {"hit": True}, f"重放未成功: {data}"
        assert calls["login"] == 2, "应恰好自动重登一次"
        assert calls["teacher"] == 2, "应恰好重放一次（401 一次 + 成功一次）"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_relogin_only_once_then_fail():
    """重登后仍 401 → 必须抛错而非死循环。"""
    calls = {"login": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/auth/login":
            calls["login"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"access_token": "tok"}})
        if path == "/system/user/getInfo":
            return httpx.Response(200, json={"code": 200, "data": {"user": {"userId": 1}}})
        return httpx.Response(401)  # /teacher/** 永远 401

    c = RuoyiClient()
    await c._client.aclose()
    c._client = httpx.AsyncClient(base_url="http://mock", transport=httpx.MockTransport(handler))
    try:
        await c.login("u", "p")
        with pytest.raises(Exception):
            await c.teacher_post("/teacher/anything", {})
        assert calls["login"] == 2, f"重登应只试一次, 实际 login 调用 {calls['login']} 次"
    finally:
        await c.aclose()
