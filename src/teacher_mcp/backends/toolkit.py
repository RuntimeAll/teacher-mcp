"""toolkit（LangGraph 举一反三，FastAPI :9093）HTTP 客户端 —— 单 client，薄代理。

PRD-O-005 批3：举一反三全链路端点脱离 book-ui 独立驱动（H2 配方实测）。
  - 入口 /variant/invoke（= /{agent_id}/invoke，agent_id=variant）：母题轮/确认轮/生成轮共用，
    多轮有状态由 thread_id 贯穿；硬停靠 agent_config 信号续跑（confirmed_chapter_id / start_variants）。
  - 确定性端点 /variant/{artifact,verify-one,edit-item,compose-figure,persist,persist-one}。

🔴 鉴权两层（H2 结论）：
  ① service 层 bearer（verify_bearer）——toolkit .env 无 AUTH_SECRET → 关闭，无需 Authorization 头。
  ② agent 内身份闸——每次 invoke 必带 agent_config.ruoyi_token（不验签只读 userId，生产传真 RuoYi token
     供落库归属/计费）。token 由注入的 RuoyiClient 会话态提供；未登录 → ToolkitAuthError。
🔴 trust_env=False：禁 httpx 读系统代理，否则连 localhost:9093 被本地代理劫持。
🔴 timeout=600s：LLM 轮 60~70s（母题读图/变式生成），远在超时内；非 SSE，/invoke 同步返回够用。
"""
from typing import Any, Optional

import httpx

from teacher_mcp.config import settings
from teacher_mcp.backends.ruoyi import RuoyiClient


class ToolkitError(Exception):
    """toolkit 调用通用异常（非 2xx / 协议异常）。"""


class ToolkitDownError(ToolkitError):
    """toolkit(:9093) 连不上/超时（ConnectError/Timeout）——工具层转 hint「起 toolkit」。"""


class ToolkitAuthError(ToolkitError):
    """未登录：拿不到 RuoYi token 注入 agent_config.ruoyi_token——工具层转 hint「先 login」。"""


class ToolkitClient:
    """举一反三端点薄代理：注入 RuoyiClient 取登录 token，确定性透传 toolkit HTTP。"""

    def __init__(self, ruoyi_client: RuoyiClient, base_url: str = "") -> None:
        self._ruoyi = ruoyi_client
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.toolkit_base_url, timeout=600.0, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ───────────────────────── 身份 ─────────────────────────
    def require_token(self) -> str:
        """取登录 RuoYi token（举一反三入口/落库归属必需）。未登录 → ToolkitAuthError。"""
        tok = self._ruoyi.token
        if not tok:
            raise ToolkitAuthError("未登录会话：请先调 login 工具（举一反三入口需真实 RuoYi token）")
        return tok

    # ───────────────────────── 调用底座 ─────────────────────────
    async def _post(self, path: str, body: dict) -> Any:
        """POST toolkit 端点。连不上/超时 → ToolkitDownError；非 2xx → ToolkitError；返回 JSON。"""
        try:
            resp = await self._client.post(path, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.TimeoutException, httpx.TransportError) as e:
            raise ToolkitDownError(f"{path}: {type(e).__name__}: {e}") from e
        if resp.status_code >= 300:
            detail = ""
            try:
                detail = str(resp.json())
            except Exception:
                detail = resp.text[:200]
            raise ToolkitError(f"{path} HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception as e:
            raise ToolkitError(f"{path} 响应非 JSON: {resp.text[:200]}") from e

    async def invoke(
        self, message: str, thread_id: str, agent_config: Optional[dict] = None
    ) -> dict:
        """POST /variant/invoke（母题/确认/生成轮共用）。自动注入 agent_config.ruoyi_token。
        返回 ChatMessage dict（{type, content, run_id, ...}；content 可为 str 或结构化 list）。"""
        cfg = dict(agent_config or {})
        cfg.setdefault("ruoyi_token", self.require_token())
        body = {"message": message, "thread_id": thread_id, "agent_config": cfg}
        return await self._post("/variant/invoke", body)

    async def variant_call(self, path: str, body: dict) -> Any:
        """通用 POST /variant/{path}（artifact/verify-one/edit-item/compose-figure/persist…）。
        path 可带或不带前导 /variant。ruoyi_token 由各确定性端点在 body 里自带（调用方负责）。"""
        p = path if path.startswith("/") else f"/variant/{path.lstrip('/')}"
        return await self._post(p, body)
