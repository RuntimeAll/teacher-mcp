"""MCP 工具·读层：list_kg_tree（整树） + resolve_kg（锚定查表，PRD-C-208 ②）。"""
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

    @mcp.tool()
    def resolve_kg(
        subject_root: str,
        query: str = "",
        section_num: str = "",
        parent_id: str = "",
        leaves_only: bool = False,
        limit: int = 50,
    ) -> dict:
        """KG 锚定查表（确定性只读，数学+科学一套通吃）：按名称/节号/父节点在某教材根下查节点，供选锚定叶子。

        🔴 叶子=无子节点（is_leaf），别按 level 判——科学 901 树 5 层、902-906 树 4 层，叶深不一（H2 实测）。
        用法：先 query 模糊（如「乘方」）看候选 → 挑 is_leaf=true 的最贴切者作 ingest_items 的 kp_id；
        同步练习类卷名带节号可 section_num 精确命中（如 "2.5" → 「2.5 有理数的乘方」节点）。
        参数:
          subject_root: 教材根 id（数学七上="100"；科学="901".."906"）——锚定范围的唯一开关
          query       : 名称模糊词（LIKE %query%）
          section_num : 节号精确匹配（如 "2.5"，命中名称以「2.5 」开头的节点）
          parent_id   : 只列某节点的直接子节点（浏览下钻用；给了它则忽略 subject_root 前缀过滤）
          leaves_only : 只返回叶子
        返回: {ok, count, nodes:[{id,name,level,parent_id,is_leaf}]}；无命中 → count=0 不报错。
        """
        if not subject_root and not parent_id:
            return {"ok": False, "reason": "subject_root 必填（数学七上=100 / 科学=901..906）"}
        from app import db
        try:
            nodes = db.kg_query(subject_root, query=query, section_num=section_num,
                                parent_id=parent_id, leaves_only=leaves_only, limit=limit)
        except Exception as e:
            return {"ok": False, "reason": f"KG 查表失败: {type(e).__name__}: {e}"}
        return {"ok": True, "count": len(nodes), "nodes": nodes}
