"""MCP·说明书层（PRD-C-208 收口 B3 + PRD-C-213 R7a 备课角色）：把角色说明书暴露进协议本身。

🔴 why：AC7 证明"README 自足"，但 README 是磁盘文件——对外开放（stdio→HTTP）后外部 agent 摸不到磁盘。
   暴露成 MCP resource + 工具双通道后，任何 agent 连上 server 即可**通过协议**拿到说明书，角色自足性闭环。
   （resource = MCP 原语，支持 resources/list 发现；tool 兜底 = 部分 client 对 resource 支持弱，工具面人人可调。）

🔴 双角色（R7a）：录入角色 = README.md（七类来源路由 + IngestItem 契约）；备课角色 = PREP_ROLE.md
   （备课线路编排 + 铁律）。get_role_manual(role) 分角色返回；两条 resource 各自可 list。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_README = _ROOT / "README.md"
_PREP = _ROOT / "PREP_ROLE.md"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        return f"（说明书读取失败: {e}）"


def _ingest_text() -> str:
    return _read(_README)


def _prep_text() -> str:
    return _read(_PREP)


def register(mcp, client=None, default_role="ingest") -> None:
    @mcp.resource("teacher://manual/ingest-role")
    def ingest_role_manual() -> str:
        """录入角色说明书（README 全文）：路由矩阵 + IngestItem 契约 + 工具契约。fresh agent 先读它再干活。"""
        return _ingest_text()

    @mcp.resource("teacher://manual/prep-role")
    def prep_role_manual() -> str:
        """备课角色说明书（PREP_ROLE 全文）：备课线路编排步骤 + 私有池铁律 + 变式补题路径。备课前先读它。"""
        return _prep_text()

    @mcp.tool()
    def get_role_manual(role: str = "") -> dict:
        """取角色说明书全文。role 分角色返回：

        - role="ingest"（默认）= 录入角色（README：七类来源路由矩阵 + IngestItem 契约面板 + 全部工具契约）。
        - role="prep" = 🔴 备课角色（PREP_ROLE：备课线路从零到可打印材料的编排步骤 + 圈题/变式/据讲义出题
          路径 + 私有池不公开等铁律）。备课前先调 role="prep"。

        🔴 首次以某身份使用本 server 的 agent 先调对应 role：说明书告诉你这条线怎么一步步走、每步调什么工具。
        返回: {ok, role, manual}（markdown 全文）。
        """
        r = (role or default_role or "ingest").strip().lower()
        if r in ("prep", "备课", "prep-role", "lesson"):
            return {"ok": True, "role": "prep", "manual": _prep_text()}
        return {"ok": True, "role": "ingest", "manual": _ingest_text()}
