"""PRD-C-1000 teacher-mcp —— RuoYi 平台能力 MCP 适配层（探针）。

本地 Claude Code 当编排层，经本 MCP server 把 RuoYi:8090 现有 HTTP 接口当工具调。
两底座（RuoYi:8090 / toolkit:8093）源码零改，本 server 是独立新增薄代理层。

🔴 加能力 = 加 tools/<name>.py + 下面 register 一行（AC6 解耦）：
   身份层(auth) / 读层(kg) / 写层(compose) 已分。二期 make_variants / ingest_textbook / build_kg
   照样各加一个 tools 模块 + register，login/鉴权/底座调用层不动。

传输 = stdio（本地 Claude Code 默认子进程最简）。
"""
import atexit

from mcp.server.fastmcp import FastMCP

from app.ruoyi import RuoyiClient
from app.tools import auth as tool_auth
from app.tools import compose as tool_compose
from app.tools import ingest as tool_ingest
from app.tools import kg as tool_kg
from app.tools import label as tool_label

mcp = FastMCP("teacher-mcp")

# 单进程单会话客户端（探针口径，stdio 单 client）。token/user_id 驻留实例。
_client = RuoyiClient()


@atexit.register
def _cleanup() -> None:
    # 进程退出时尽量关连接（事件循环已停时忽略异常）
    try:
        import anyio

        anyio.run(_client.aclose)
    except Exception:
        pass


# ── 工具注册（加能力 = 在此 register 一行）──
tool_auth.register(mcp, _client)        # login（身份层）
tool_kg.register(mcp, _client)          # list_kg_tree（读层）
tool_compose.register(mcp, _client)     # compose_paper（写层·探针能力①组卷）
tool_ingest.register(mcp, _client)      # format_question / upload_image / ingest_question（写层·能力②录入）
tool_label.register(mcp, _client)       # label_question（打标层·能力③ DNA 打标：难度/锚/解法骨架/变式底料）
# 二期占位（同样「加一个 tools 目录 + register 一行」）：
#   from app.tools import variants as tool_variants; tool_variants.register(mcp, _client)   # make_variants（举一反三）
#   from app.tools import kgbuild as tool_kgbuild; tool_kgbuild.register(mcp, _client)       # build_kg


def main() -> None:
    mcp.run()  # 默认 stdio transport


if __name__ == "__main__":
    main()
