"""MCP·说明书层（PRD-C-208 收口 B3）：把角色说明书暴露进协议本身。

🔴 why：AC7 证明"README 自足"，但 README 是磁盘文件——对外开放（stdio→HTTP）后外部 agent 摸不到磁盘。
   暴露成 MCP resource + 工具双通道后，任何 agent 连上 server 即可**通过协议**拿到说明书，角色自足性闭环。
   （resource = MCP 原语，支持 resources/list 发现；tool 兜底 = 部分 client 对 resource 支持弱，工具面人人可调。）
"""
from pathlib import Path

_README = Path(__file__).resolve().parent.parent.parent / "README.md"


def _manual_text() -> str:
    try:
        return _README.read_text(encoding="utf-8")
    except OSError as e:
        return f"（说明书读取失败: {e}）"


def register(mcp, client=None) -> None:
    @mcp.resource("teacher://manual/ingest-role")
    def ingest_role_manual() -> str:
        """录入角色说明书（README 全文）：路由矩阵 + IngestItem 契约 + 工具契约。fresh agent 先读它再干活。"""
        return _manual_text()

    @mcp.tool()
    def get_role_manual() -> dict:
        """取录入角色说明书全文（README：七类来源路由矩阵 + IngestItem 契约面板 + 全部工具契约）。

        🔴 首次使用本 server 的 agent 先调这个：说明书告诉你什么来源走哪条工具链、kp_id 怎么查、图怎么占位。
        返回: {ok, manual}（markdown 全文）。
        """
        return {"ok": True, "manual": _manual_text()}
