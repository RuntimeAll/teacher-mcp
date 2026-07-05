"""PRD-C-1000 teacher-mcp —— RuoYi 平台能力 MCP 适配层（探针）。

本地 Claude Code 当编排层，经本 MCP server 把 RuoYi:8090 现有 HTTP 接口当工具调。
两底座（RuoYi:8090 / toolkit:8093）源码零改，本 server 是独立新增薄代理层。

🔴 多底座路由（RuoyiCluster）：A=:8080 主底座（现有全部工具打它）/ C=:8090 讲义接口待接
（同库同账号、token 互不通用 → 各自登录，C 线懒登录）/ toolkit=:8093 占位（FastAPI 非 RuoYi，未实现 client）。

🔴 加能力 = 加 tools/<name>.py + 下面 register 一行（AC6 解耦）：
   身份层(auth) / 读层(kg) / 写层(compose) 已分。二期 make_variants / ingest_textbook / build_kg
   照样各加一个 tools 模块 + register，login/鉴权/底座调用层不动。

传输 = stdio（本地 Claude Code 默认子进程最简）。
"""
import atexit

from mcp.server.fastmcp import FastMCP

from app.ruoyi import RuoyiCluster
from app.tools import auth as tool_auth
from app.tools import compose as tool_compose
from app.tools import convert as tool_convert
from app.tools import ingest as tool_ingest
from app.tools import kg as tool_kg
from app.tools import label as tool_label
from app.tools import lecture as tool_lecture
from app.tools import manual as tool_manual
from app.tools import schedule as tool_schedule

mcp = FastMCP("teacher-mcp")

# 多底座会话簇（stdio 单进程）：_cluster.a=A 线主底座（现有工具全打它），_cluster.c=C 线懒登录。
_cluster = RuoyiCluster()


@atexit.register
def _cleanup() -> None:
    # 进程退出时尽量关连接（事件循环已停时忽略异常）
    try:
        import anyio

        anyio.run(_cluster.aclose)
    except Exception:
        pass


# ── 工具注册（加能力 = 在此 register 一行）──
tool_auth.register(mcp, _cluster)         # login（身份层：登 A 线 + 记凭据供 C 线懒登录，传 cluster）
tool_kg.register(mcp, _cluster.a)         # list_kg_tree / resolve_kg（读层：整树 + 锚定查表）
tool_compose.register(mcp, _cluster.a)    # compose_paper / create_paper / update_paper（写层·能力①组卷）
tool_ingest.register(mcp, _cluster.a)     # format/upload/ingest_question + 🔴ingest_items 统一入库口（写层·能力②录入，PRD-C-208）
tool_label.register(mcp, _cluster.a)      # label_question（打标层·能力③ DNA 打标：难度/锚/解法骨架/变式底料）
tool_convert.register(mcp, _cluster.a)    # convert_doc / convert_pdf / parse_paper_text（转换层·确定性预处理，PRD-C-208）
tool_manual.register(mcp, _cluster.a)     # 角色说明书进协议：resource teacher://manual/ingest-role + get_role_manual 工具（B3）
tool_lecture.register(mcp, _cluster)      # convert_lecture_docx（确定性转换）+ save_lecture_frag（写层·能力④讲义录入，PRD-C-210）🔴打 C 线 :8090=传 _cluster，工具内 ensure_c() 懒登录
tool_schedule.register(mcp, _cluster)     # 教学安排与备课闭环 10 工具（PRD-C-213）🔴全打 C 线 :8090=传 _cluster，工具内 ensure_c() 懒登录
# 二期占位（同样「加一个 tools 目录 + register 一行」）：
#   from app.tools import variants as tool_variants; tool_variants.register(mcp, _cluster.a)   # make_variants（举一反三）
#   from app.tools import kgbuild as tool_kgbuild; tool_kgbuild.register(mcp, _cluster.a)       # build_kg


def main() -> None:
    mcp.run()  # 默认 stdio transport


if __name__ == "__main__":
    main()
