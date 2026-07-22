"""teacher-mcp 配置 —— 从 .env 读（pydantic-settings）。凭据不硬编码、不入 git。

🔴 env_file 用绝对路径（基于本文件定位），免受 MCP server 被 stdio client 以任意 cwd 拉起的影响。
🔴 PRD-O-005 重建：A/C 双线已合并为同一服务（默认 :9090）；单 RuoyiClient，无 Cluster。
🔴 双实例（2026-07-22）：env TEACHER_MCP_ENV_FILE 可指定替代 env 文件（如 prod 连接配置，
   真凭据落 workplace 外 password/ 目录不入任何 git）——同一份代码起 localhost/prod 两个 MCP 实例。
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓根 = src/teacher_mcp/config.py 往上三层；TEACHER_MCP_ENV_FILE 显式指定时优先（双实例支撑）
_ENV = Path(os.environ.get("TEACHER_MCP_ENV_FILE") or
            Path(__file__).resolve().parent.parent.parent / ".env")


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

    # PRD-007 CLI 驱动版·后端锁身份（防提示注入）：飞书 bot 每次调 headless claude CLI 前
    # 设 env BOUND_OPENID=<已核验的发送者 open_id>，CLI 透传给本 MCP 子进程。
    # 🔴 非空 → ①RuoyiClient 首次需登录态时自动 login_as(bound_openid) 走 botLogin（绝不用用户名密码）；
    #         ②login/login_as 两工具对模型隐藏，模型无法自行切身份（用户消息里写「用 admin 身份」也无效）。
    # 🔴 为空 → 行为完全不变（login/login_as 照常注册，SDK/交互路径不受影响）。
    bound_openid: str = ""

    # toolkit（LangGraph 举一反三，FastAPI 非 RuoYi，:9093）——health_check 探针用
    toolkit_base_url: str = "http://localhost:9093"

    # PRD-009 课后反馈：export_feedback_png 把家长版 PNG 下载落到本机的目录（bot 读本地文件免鉴权）。
    # 🔴 /teacher/schedule/artifact 是 @SaCheckLogin，故由持 token 的 MCP 下载写盘，非 bot 裸下载。
    feedback_out_dir: str = "/tmp"

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
