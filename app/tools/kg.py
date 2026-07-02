"""MCP 工具·读层：list_kg_tree（查知识点树，供编排层选知识点锚定组卷）。"""
from app.ruoyi import RuoyiClient, RuoyiError


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool()
    async def list_kg_tree() -> dict:
        """查平台知识点树（组卷的知识点白名单源）。返回顶层节点 + children 嵌套。

        编排层（Claude Code）据此选要考查的知识点叶子 id，喂给 compose_paper 的 outline.subjectId。
        返回: {ok, nodes:[{id,name,children?}, ...]}；空树 → {ok:true, nodes:[]} 不报错。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            tree = await client.lazy_tree({})
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        if isinstance(tree, dict):
            tree = tree.get("rows") or tree.get("nodes") or []
        if not isinstance(tree, list):
            tree = []
        return {"ok": True, "nodes": tree}
