"""RuoYi 底座（C 线 book-server :8090）HTTP 客户端。

搬运自 ai-orchestrator/app/clients/ruoyi.py，PRD-C-1000 改动两点：
  1. login 接收**任意 teacher 用户名/密码**（真账号身份贯穿），不再写死 .env 单账号。
  2. login 后取该账号 userId（/system/user/getInfo），作 compose 的 teacherId → biz_paper.create_by 归属该 teacher。

🔴 双头铁律：调 /teacher/** 必带 Authorization Bearer + clientid，缺 clientid 必 401。
🔴 misikt envelope：/teacher/** 响应被全局 advice 重写成 {code:1, message, response}，按 code==1 取 response。
   （/auth/**、/system/** 不被重写，保持 RuoYi 原样 {code:200, msg, data}。）
🔴 trust_env=False：禁 httpx 读系统 HTTP(S)_PROXY，否则调 localhost:8090 会被本地代理吞掉超时。
"""
from typing import Any, Optional

import httpx

from app.config import settings


class RuoyiError(Exception):
    pass


class RuoyiClient:
    """单进程单会话（探针口径）：login 后 token/user_id 驻留实例，后续工具隐式带身份。

    多 client/多会话的 token_ref 句柄隔离是后续卡的事；本期 stdio 单 client 单会话足够。
    """

    def __init__(self, base_url: str = "") -> None:
        # base_url 空 = 沿用 settings.ruoyi_base_url（A 线主底座）；token/user_id/username 均实例级，各底座各持会话
        self._token: Optional[str] = None
        self._user_id: Optional[int] = None
        self._username: Optional[str] = None
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.ruoyi_base_url, timeout=60.0, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ───────────────────────── 会话态 ─────────────────────────
    @property
    def user_id(self) -> Optional[int]:
        return self._user_id

    @property
    def username(self) -> Optional[str]:
        return self._username

    def has_session(self) -> bool:
        return bool(self._token)

    # ───────────────────────── 登录 + 取身份 ─────────────────────────
    async def login(self, username: str, password: str) -> dict:
        """真账号登录拿 access_token + 取 userId。/auth/login 不走 envelope，返回 RuoYi 原样。"""
        if not username or not password:
            raise RuoyiError("login 需用户名+密码（或在 .env 配 RUOYI_USERNAME/RUOYI_PASSWORD 兜底）")
        body = {
            "clientId": settings.ruoyi_client_id,
            "grantType": "password",
            "tenantId": settings.ruoyi_tenant_id,
            "username": username,
            "password": password,
        }
        resp = await self._client.post(
            "/auth/login", json=body, headers={"clientid": settings.ruoyi_client_id}
        )
        try:
            data = resp.json()
        except Exception:
            raise RuoyiError(f"登录响应非 JSON: status={resp.status_code} body={resp.text[:200]}")
        if data.get("code") != 200:
            raise RuoyiError(f"登录失败 code={data.get('code')} msg={data.get('msg')}")
        token = (data.get("data") or {}).get("access_token")
        if not token:
            raise RuoyiError(f"登录返回无 access_token: {data}")
        self._token = token
        self._username = username
        self._user_id = await self._fetch_user_id()
        return {"user_id": self._user_id, "username": self._username}

    async def _fetch_user_id(self) -> Optional[int]:
        """取登录账号 userId。GET /system/user/getInfo（RuoYi 原样 {code:200,data:{user:{userId}}}）。"""
        resp = await self._client.get("/system/user/getInfo", headers=self._headers())
        try:
            data = resp.json()
        except Exception:
            raise RuoyiError(f"getInfo 响应非 JSON: status={resp.status_code} body={resp.text[:200]}")
        if data.get("code") != 200:
            raise RuoyiError(f"getInfo 非 200: code={data.get('code')} msg={data.get('msg')}")
        # 防御取值：data.user.userId / data.data.user.userId / data.userId
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        user = d.get("user") if isinstance(d, dict) else None
        uid = None
        if isinstance(user, dict):
            uid = user.get("userId")
        if uid is None and isinstance(d, dict):
            uid = d.get("userId")
        if uid is None:
            raise RuoyiError(f"getInfo 未取到 userId，原始: {str(data)[:300]}")
        try:
            return int(uid)
        except Exception:
            return uid  # type: ignore[return-value]

    # ───────────────────────── 通用调用 ─────────────────────────
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "clientid": settings.ruoyi_client_id,
            "Content-Type": "application/json",
        }

    async def teacher_post(self, path: str, body: Optional[dict] = None) -> Any:
        """调 /teacher/** 接口，解 envelope（code==1 取 response）。需先 login。"""
        if not self._token:
            raise RuoyiError("未登录会话：请先调 login 工具")
        resp = await self._client.post(path, json=body or {}, headers=self._headers())
        if resp.status_code == 401:
            raise RuoyiError(f"{path} 401：会话失效，请重新 login")
        try:
            data = resp.json()
        except Exception:
            raise RuoyiError(f"{path} 响应非 JSON: status={resp.status_code} body={resp.text[:200]}")
        if data.get("code") != 1:
            msg = data.get("message") or data.get("msg")
            raise RuoyiError(f"{path} 非 code==1: code={data.get('code')} msg={msg}")
        return data.get("response")

    async def lazy_tree(self, body: Optional[dict] = None) -> Any:
        """拉知识点树（组卷白名单源）。POST /teacher/question/lazyTree。"""
        return await self.teacher_post("/teacher/question/lazyTree", body or {})

    async def auto_generate(self, body: dict) -> Any:
        """确定性组卷接口。POST /teacher/paper/auto-generate。save=true+teacherId 才落库 biz_paper。"""
        return await self.teacher_post("/teacher/paper/auto-generate", body)


class RuoyiCluster:
    """多底座会话簇：A=:8080 主底座（现有全部工具打它）+ C=:8090（讲义接口，后续卡用）。

    两 BE 同库同账号体系（sys_user 同表、同一套用户名密码），但 token 互不通用（两套
    Sa-Token 会话）→ 每底座各自 login、各持 token。
    - login(): 登 A 线并记住凭据（本地进程内明文即可）。
    - ensure_c(): 首次需要 C 线时用记住的凭据对 C 线懒登录——C 线没起时不拖垮 A 线角色。
    - toolkit :8093 是 FastAPI 非 RuoYi，仅 settings.toolkit_base_url 占位，本期不实现 client。
    """

    def __init__(self) -> None:
        self.a = RuoyiClient()  # A 线主底座（settings.ruoyi_base_url，:8080）
        self._c: Optional[RuoyiClient] = None  # C 线懒创建
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    @property
    def c(self) -> RuoyiClient:
        """C 线客户端（:8090，懒创建；会话需另经 ensure_c() 登录）。"""
        if self._c is None:
            self._c = RuoyiClient(base_url=settings.ruoyi_c_base_url)
        return self._c

    async def login(self, username: str, password: str) -> dict:
        """登 A 线并记住凭据（供 C 线懒登录复用，同库同账号）。"""
        info = await self.a.login(username, password)
        self._username = username
        self._password = password
        return info

    async def ensure_c(self) -> RuoyiClient:
        """确保 C 线已登录（懒登录）：无会话则用记住的凭据对 C 线 login。"""
        c = self.c
        if not c.has_session():
            if not self._username or not self._password:
                raise RuoyiError("C 线懒登录失败：请先调 login（A 线登录时记住凭据）")
            await c.login(self._username, self._password)
        return c

    async def aclose(self) -> None:
        await self.a.aclose()
        if self._c is not None:
            await self._c.aclose()
