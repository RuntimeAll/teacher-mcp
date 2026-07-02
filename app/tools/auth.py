"""MCP 工具·身份层：login。

🔴 加能力 = 加一个 tools/<name>.py + 在 server.py register（AC6 解耦）。
身份层与具体业务能力（组卷/举一反三/录入/KG）隔离：业务工具只隐式取会话身份，不重复登录逻辑。
"""
from app.config import settings
from app.ruoyi import RuoyiClient, RuoyiError


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool()
    async def login(username: str = "", password: str = "") -> dict:
        """以真实 teacher 账号登录平台，拿双头 token 注入本会话身份（后续所有工具隐式带该身份、落 RuoYi 权限审计）。

        参数:
          username/password: teacher 账号。留空则用 .env 兜底（RUOYI_USERNAME/RUOYI_PASSWORD）。
        返回:
          {ok, teacher_id, username} —— token 仅驻留 server 侧会话态，不回吐明文给调用方。
        """
        u = username or settings.ruoyi_username
        p = password or settings.ruoyi_password
        try:
            info = await client.login(u, p)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        return {"ok": True, "teacher_id": info["user_id"], "username": info["username"]}
