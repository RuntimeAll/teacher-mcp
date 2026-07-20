"""teacher-mcp 配置 —— 从 .env 读（pydantic-settings）。凭据不硬编码、不入 git。

🔴 env_file 用绝对路径（基于本文件定位），免受 MCP server 被 stdio client 以任意 cwd 拉起的影响。
🔴 PRD-O-005 重建：A/C 双线已合并为同一服务（默认 :9090）；单 RuoyiClient，无 Cluster。
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓根 = src/teacher_mcp/config.py 往上三层
_ENV = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV), env_file_encoding="utf-8", extra="ignore"
    )

    # RuoYi 底座（合并后 book-server :9090，master-ai）
    ruoyi_base_url: str = "http://localhost:9090"
    ruoyi_client_id: str = "e5cd7e4891bf95d1d19206ce24a7b32e"
    ruoyi_tenant_id: str = "000000"

    # PRD-007 飞书机器人免密切身份：/auth/botLogin 的服务密钥（X-Bot-Secret）。
    # 🔴 只落 env、只在机器人后端持有，绝不入 git；未配置 → login_as 直接软拒绝提示。
    bot_secret: str = ""

    # toolkit（LangGraph 举一反三，FastAPI 非 RuoYi，:9093）——health_check 探针用
    toolkit_base_url: str = "http://localhost:9093"

    # 真账号身份：正式由 login 工具传参；以下仅 login 不传参时兜底 + 冒烟用
    ruoyi_username: str = ""
    ruoyi_password: str = ""

    # dev 库直连（模型链/难度依据/卷目录/KG查表 pymysql 收口，见 backends/db.py 头注释）
    db_host: str = "127.0.0.1"
    db_port: int = 3307
    db_user: str = "root"
    db_password: str = "123456"
    db_database: str = "ai_lesson_prep"


settings = Settings()
