"""RuoYi 底座（合并后 book-server :9090）HTTP 客户端 —— 单 client 单会话。

PRD-O-005 重建：A/C 双线已合并为同一服务，删 RuoyiCluster，只留单 RuoyiClient。
  1. login 接收**任意 teacher 用户名/密码**（真账号身份贯穿），登录成功后把凭据存实例（仅内存）。
  2. login 后取该账号 userId（/system/user/getInfo），作 compose 的 teacherId → biz_paper.create_by 归属该 teacher。
  3. 🔴 AC5 可用性：_teacher_call 遇 401 → 若有存凭据自动重登一次再重放请求（只试一次防死循环），仍失败才抛。

🔴 双头铁律：调 /teacher/** 必带 Authorization Bearer + clientid，缺 clientid 必 401。
🔴 misikt envelope：/teacher/** 响应被全局 advice 重写成 {code:1, message, response}，按 code==1 取 response。
   （/auth/**、/system/** 不被重写，保持 RuoYi 原样 {code:200, msg, data}。）
🔴 trust_env=False：禁 httpx 读系统 HTTP(S)_PROXY，否则调 localhost:9090 会被本地代理吞掉超时。
"""
from typing import Any, Optional

import httpx

from teacher_mcp.config import settings


class RuoyiError(Exception):
    pass


class RuoyiClient:
    """单进程单会话：login 后 token/user_id/凭据驻留实例，后续工具隐式带身份。

    401 自动重登：会话过期时用存下的凭据重登一次再重放，对调用方透明（AC5）。
    """

    def __init__(self, base_url: str = "") -> None:
        self._token: Optional[str] = None
        self._user_id: Optional[int] = None
        self._username: Optional[str] = None
        self._password: Optional[str] = None  # 🔴 仅内存，供 401 自动重登
        self._openid: Optional[str] = None  # 🔴 PRD-007 免密身份：非空则 401 走 botLogin 重签而非密码重登
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.ruoyi_base_url, timeout=60.0, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ───────────────────────── 会话态 ─────────────────────────
    @property
    def token(self) -> Optional[str]:
        """只读暴露登录 access_token（供 toolkit 举一反三入口注入 agent_config.ruoyi_token）。未登录=None。"""
        return self._token

    @property
    def user_id(self) -> Optional[int]:
        return self._user_id

    @property
    def username(self) -> Optional[str]:
        return self._username

    @property
    def current_openid(self) -> Optional[str]:
        """当前免密身份的飞书 open_id（PRD-007）。None = 非免密态（普通用户名密码登录 / 未登录）。"""
        return self._openid

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
        self._password = password  # 🔴 存凭据供 401 自动重登
        self._openid = None  # 🔴 普通登录 = 离开免密态，清 openid（否则 401 会错走 botLogin 重签）
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

    # ───────────────────────── PRD-007 免密切身份 ─────────────────────────
    async def login_as(self, openid: str) -> dict:
        """飞书机器人免密签发：凭服务密钥 X-Bot-Secret 调 /auth/botLogin，用 openid 换该 teacher 的 token。

        成功 → 替换 _token/_user_id、记录 _openid（供 401 重签）；免密身份不留用户名密码。
        /auth/botLogin 不走 envelope，返回 RuoYi 原样 {code, msg, data:{access_token,user_id}}。
        🔴 openid 未绑定 → BE 返 code!=200 且 msg 含「未绑定」，此处原样带进 RuoyiError.msg（供上层路由拒绝话术）。
        """
        if not openid:
            raise RuoyiError("login_as 需 openid（飞书消息发送者 open_id）")
        if not settings.bot_secret:
            raise RuoyiError("BOT_SECRET 未配置：请在机器人后端 .env 配服务密钥 BOT_SECRET（不入 git）")
        body: dict = {"openid": openid}
        if settings.ruoyi_client_id:
            body["clientId"] = settings.ruoyi_client_id
        resp = await self._client.post(
            "/auth/botLogin",
            json=body,
            headers={"X-Bot-Secret": settings.bot_secret, "clientid": settings.ruoyi_client_id},
        )
        try:
            data = resp.json()
        except Exception:
            raise RuoyiError(f"botLogin 响应非 JSON: status={resp.status_code} body={resp.text[:200]}")
        if data.get("code") != 200:
            raise RuoyiError(f"botLogin 失败 code={data.get('code')} msg={data.get('msg')}")
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = d.get("access_token")
        if not token:
            raise RuoyiError(f"botLogin 返回无 access_token: {str(data)[:200]}")
        self._token = token
        self._openid = openid
        self._username = None  # 免密身份：不留用户名密码，401 一律走 openid 重签
        self._password = None
        uid = d.get("user_id")
        try:
            self._user_id = int(uid) if uid is not None else None
        except (TypeError, ValueError):
            self._user_id = uid  # type: ignore[assignment]
        return {"user_id": self._user_id, "openid": openid}

    async def _resign(self) -> bool:
        """401 自动恢复调度：免密态(_openid 非空)→ 重调 botLogin 重签；否则回落用户名密码重登。均失败 → False。"""
        if self._openid:
            try:
                await self.login_as(self._openid)
                return True
            except RuoyiError:
                return False
        return await self._relogin()

    async def _relogin(self) -> bool:
        """用存下的凭据重登一次（401 自动恢复兜底）。无存凭据 → False；重登异常 → False（由调用方抛原始 401）。"""
        if not self._username or not self._password:
            return False
        try:
            await self.login(self._username, self._password)
            return True
        except RuoyiError:
            return False

    # ───────────────────────── 通用调用 ─────────────────────────
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "clientid": settings.ruoyi_client_id,
            "Content-Type": "application/json",
        }

    async def _teacher_call(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        _retry: bool = True,
    ) -> Any:
        """调 /teacher/** 接口通用底座，解 envelope（code==1 取 response）。需先 login。

        POST/PUT 带 json=body；GET 带 params（query）。envelope 口径三线（POST/GET/PUT）统一。
        🔴 401 → 自动重签一次再重放（_retry=False 防死循环）：免密态用 openid 重调 botLogin，
           否则用存下的用户名密码重登；仍失败才抛。
        """
        if not self._token:
            raise RuoyiError("未登录会话：请先调 login 工具")
        kwargs: dict = {"headers": self._headers()}
        if params is not None:
            kwargs["params"] = params
        if method.upper() in ("POST", "PUT"):
            kwargs["json"] = body or {}
        resp = await self._client.request(method.upper(), path, **kwargs)
        if resp.status_code == 401:
            # 🔴 免密态(_openid 非空)优先按 openid 重调 botLogin 重签；否则回落用户名密码重登（_resign 内分派）
            if _retry and await self._resign():
                return await self._teacher_call(method, path, body=body, params=params, _retry=False)
            raise RuoyiError(f"{path} 401：会话失效且自动重登未成功，请重新 login / login_as")
        try:
            data = resp.json()
        except Exception:
            raise RuoyiError(f"{path} 响应非 JSON: status={resp.status_code} body={resp.text[:200]}")
        if data.get("code") != 1:
            msg = data.get("message") or data.get("msg")
            raise RuoyiError(f"{path} 非 code==1: code={data.get('code')} msg={msg}")
        return data.get("response")

    async def teacher_post(self, path: str, body: Optional[dict] = None) -> Any:
        """调 /teacher/** 接口（POST），解 envelope（code==1 取 response）。需先 login。"""
        return await self._teacher_call("POST", path, body=body or {})

    async def teacher_put(self, path: str, body: Optional[dict] = None) -> Any:
        """调 /teacher/** 接口（PUT，改期/改绑/改基本维等），解 envelope。需先 login。"""
        return await self._teacher_call("PUT", path, body=body or {})

    async def teacher_get(self, path: str, params: Optional[dict] = None) -> Any:
        """调 /teacher/** 接口（GET，卡片墙/月历/详情等），解 envelope。需先 login。"""
        return await self._teacher_call("GET", path, params=params or {})

    async def teacher_delete(self, path: str) -> Any:
        """调 /teacher/** 接口（DELETE，硬删场次等），解 envelope。需先 login。"""
        return await self._teacher_call("DELETE", path)

    async def lazy_tree(self, body: Optional[dict] = None) -> Any:
        """拉知识点树（组卷白名单源）。POST /teacher/question/lazyTree。"""
        return await self.teacher_post("/teacher/question/lazyTree", body or {})

    async def auto_generate(self, body: dict) -> Any:
        """确定性组卷接口。POST /teacher/paper/auto-generate。save=true+teacherId 才落库 biz_paper。"""
        return await self.teacher_post("/teacher/paper/auto-generate", body)
