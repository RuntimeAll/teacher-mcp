"""PRD-O-005 teacher-mcp（重建）—— 老师系统操作全权代理 MCP。

本地 CLI agent 当编排层，经本 MCP server 把合并后的 book-server（:9090）现有 HTTP 接口当工具调。
A/C 双线已合并为同一服务 → 单 RuoyiClient（无 Cluster）。

🔴 角色视图（FastMCP v3 tag 过滤）：按 env TEACHER_MCP_ROLE 只暴露本职工具组。
   build_server(role) 工厂：建 FastMCP → 各 tools 模块 register(mcp, client) →
   非 all 则 mcp.enable(tags=角色tag集, only=True)（暴露期过滤，spike 已验证）。
   缺省 role=all = 全量 34 工具 + health_check（向后兼容）。

传输 = stdio（本地 CLI agent 默认子进程最简）。
"""
import atexit
import os

from fastmcp import FastMCP

from teacher_mcp.backends.ruoyi import RuoyiClient
from teacher_mcp.backends.toolkit import ToolkitClient
from teacher_mcp.tools import data_lecture as tool_lecture
from teacher_mcp.tools import data_qbank as tool_qbank
from teacher_mcp.tools import prep as tool_prep
from teacher_mcp.tools import shared as tool_shared
from teacher_mcp.tools import variant as tool_variant

# ── ROLE → 暴露 tag 集合（None = 全量，不过滤）──
ROLE_TAGS = {
    "all": None,
    "data": {"shared", "data"},
    "prep": {"shared", "prep"},
    "variant": {"shared", "variant"},
    "ingest": {"shared", "ingest"},
    "lecture": {"shared", "lecture"},
}

# get_role_manual 缺省角色跟随 ROLE：prep→prep.md，variant→variant.md，其余→data.md
_MANUAL_ROLE = {
    "prep": "prep", "variant": "variant",
    "all": "data", "data": "data", "ingest": "data", "lecture": "data",
}


def build_server(role: str = "all") -> FastMCP:
    """按角色建 FastMCP 实例。role 缺省 all=全量；非 all 则 enable(only=True) 只暴露本职组。"""
    role = (role or "all").strip().lower() or "all"
    tags = ROLE_TAGS.get(role, None)
    manual_role = _MANUAL_ROLE.get(role, "data")

    mcp = FastMCP("teacher-mcp")
    client = RuoyiClient()  # 单会话（stdio 单进程）；A/C 已合并 :9090
    toolkit = ToolkitClient(client)  # 举一反三底座（:9093）；注入 client 取登录 token

    @atexit.register
    def _cleanup() -> None:
        try:
            import anyio
            anyio.run(client.aclose)
            anyio.run(toolkit.aclose)
        except Exception:
            pass

    # ── 工具注册（加能力 = 在此 register 一行）──
    tool_shared.register(mcp, client, manual_role)   # login/list_kg_tree/resolve_kg/search/get/manual + health_check
    tool_qbank.register(mcp, client)                 # 录题组（convert/format/ingest/verify/label）
    tool_lecture.register(mcp, client)               # 讲义组（convert_lecture_docx/save/remove/list/get）
    tool_prep.register(mcp, client)                  # 备课组（schedule 11 + compose/create/update_paper）
    tool_variant.register(mcp, client, toolkit)      # 举一反三组（make_variants 等 7，tags={"variant"}）

    # ── 角色过滤（暴露期，only=True）──
    if tags is not None:
        mcp.enable(tags=tags, only=True)

    return mcp


def main() -> None:
    role = os.getenv("TEACHER_MCP_ROLE", "all")
    build_server(role).run()  # 默认 stdio transport


if __name__ == "__main__":
    main()
