"""MCP 工具·书架组（shelf 角色）——PRD-002 六工具（源=A 线草稿，已并入 teacher-mcp）。

工具全部薄包 book-server 的 `/teacher/shelf/**` 端点（ruoyi-book ShelfController）。
BASE_URL 由 RuoyiClient 走 .env RUOYI_BASE_URL 配（不硬编码端口）。

工具面（PRD-002 §5 / §5 gate G5）：
  create_book / list_books / get_book_structure / add_book_node / add_book_item / override_item

契约要点：
  - 所有雪花号 id 一律 **str** 收发（questionId/bookId/nodeId/itemId），防 JSON number 截尾
    （对齐 create-paper-snowflake-truncation-trap 记忆）。
  - override/explain 为自由 JSON（override={stem?,options?[]}；explain={title?,text?}），
    BE 原样存取；含数学 `<>` 不会被剥（xss.excludeUrls 已含 /teacher/shelf/**）。
  - 归属：ShelfService 内建 owner_id=当前登录 teacher；越权读写 BE 兜底 403/404。
"""
from typing import Optional

from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/shelf"


class OverridePayload(BaseModel):
    """书内改题副本（D3）：只影响本书，题库原子题不动。"""
    stem: Optional[str] = Field(default=None, description="改后题面（可含数学 <>）")
    options: Optional[list[str]] = Field(default=None, description="改后选项，如 ['A. 1','B. 2']")


def register(mcp, client: RuoyiClient) -> None:

    # ───────────────── 书 ─────────────────
    @mcp.tool(tags={"shelf"})
    async def create_book(title: str, book_type: str = "workbook",
                          subject_id: str = "", grade: str = "", edition: str = "") -> dict:
        """新建一本空书（起步）。book_type: lecture讲义型 / workbook练习册型 / special专项。

        返回 {ok, book_id(str)}；随后用 add_book_node + add_book_item 建目录树与内容，
        或整树一次建书走 import（B 线录入直出书交接面，本工具面不含 import）。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not title.strip():
            return {"ok": False, "reason": "title 不能为空"}
        body = {"title": title, "bookType": book_type,
                "subjectId": subject_id or None, "grade": grade or None, "edition": edition or None}
        try:
            resp = await client.teacher_post(f"{BASE}/book", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"建书失败: {e}"}
        return {"ok": True, "book_id": str(resp.get("id")) if isinstance(resp, dict) else None}

    @mcp.tool(tags={"shelf"})
    async def list_books(book_type: str = "", subject_id: str = "", status: str = "") -> dict:
        """我的书列表（owner 归属自动过滤 + type/subject/status 可选筛选）。

        返回 {ok, books:[{id,bookType,title,subjectId,grade,nodeCount,questionCount,itemCount,...}]}。
        统计字段（nodeCount/questionCount/itemCount）供书架卡片 'N 节 · M 题' 展示。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        params = {k: v for k, v in
                  {"bookType": book_type, "subjectId": subject_id, "status": status}.items() if v}
        try:
            resp = await client.teacher_get(f"{BASE}/book/page", params)
        except RuoyiError as e:
            return {"ok": False, "reason": f"列表失败: {e}"}
        rows = (resp.get("rows") or resp.get("list") or []) if isinstance(resp, dict) else []
        return {"ok": True, "books": rows, "total": resp.get("total") if isinstance(resp, dict) else len(rows)}

    @mcp.tool(tags={"shelf"})
    async def get_book_structure(book_id: str) -> dict:
        """书结构整树（目录树 + 各节点内容项，一次返回可渲染）。

        返回 {ok, book, tree:[{id,name,nodeType,kpId?,items:[{id,kind,questionId?,override?,explain?}],children:[...]}]}。
        override 优先于题库原题面渲染；kind=explain 走 explain.title/text。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            resp = await client.teacher_get(f"{BASE}/book/{book_id}/structure")
        except RuoyiError as e:
            return {"ok": False, "reason": f"取结构失败: {e}"}
        if not isinstance(resp, dict):
            return {"ok": False, "reason": "结构返回异常"}
        return {"ok": True, "book": resp.get("book"), "tree": resp.get("tree") or []}

    # ───────────────── 节点 ─────────────────
    @mcp.tool(tags={"shelf"})
    async def add_book_node(book_id: str, name: str, node_type: str = "sec",
                            parent_id: str = "", seq: int = 0, kp_id: str = "") -> dict:
        """给书加一个目录节点。node_type 自由值（chapter章/lecture讲/qtype_group题型组/tier难度档/sec区块…）。

        parent_id 省 = 根层节点；kp_id 可选（KG 锚，仅标签，与树结构解耦 D8）。
        节点名 name 卷面可见——🔴 禁内部词（层/★/素材/薄弱），只写干净知识点名。
        返回 {ok, node_id(str)}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not name.strip():
            return {"ok": False, "reason": "name 不能为空"}
        body = {"bookId": int(book_id), "name": name, "nodeType": node_type, "seq": seq,
                "parentId": int(parent_id) if parent_id else None,
                "kpId": int(kp_id) if kp_id else None}
        try:
            resp = await client.teacher_post(f"{BASE}/node", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"加节点失败: {e}"}
        return {"ok": True, "node_id": str(resp.get("id")) if isinstance(resp, dict) else None}

    # ───────────────── 内容项 ─────────────────
    @mcp.tool(tags={"shelf"})
    async def add_book_item(node_id: str, kind: str = "question",
                            question_id: str = "", seq: int = 0,
                            explain_title: str = "", explain_text: str = "") -> dict:
        """给节点加一个内容项。kind=question 题引用（传 question_id）/ explain 讲解块（传 explain_*）。

        🔴 question_id 一律 str（雪花号 JSON number 会截尾）。讲解块内容书自持（D2，不引用 KG 讲义层）。
        返回 {ok, item_id(str)}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        body: dict = {"nodeId": int(node_id), "kind": kind, "seq": seq}
        if kind == "question":
            if not question_id:
                return {"ok": False, "reason": "kind=question 需 question_id"}
            body["questionId"] = str(question_id)      # 🔴 str 防截尾
        elif kind == "explain":
            body["explain"] = {"title": explain_title or None, "text": explain_text or None}
        else:
            return {"ok": False, "reason": f"未知 kind: {kind}（question|explain）"}
        try:
            resp = await client.teacher_post(f"{BASE}/item", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"加内容项失败: {e}"}
        return {"ok": True, "item_id": str(resp.get("id")) if isinstance(resp, dict) else None}

    # ───────────────── 课次 ↔ 书章节材料位 ─────────────────
    @mcp.tool(tags={"prep"})
    async def bind_book_node_to_lesson(lesson_id: str, node_id: str = "", action: str = "bind") -> dict:
        """把书籍章节（书架书目录节点）绑到课次材料位，或解绑，或查本课已绑书章节。

        备课态口径（2026-07-15 扩展）：「有专项**或有书章节**=已备好」——绑上任一书章节，
        课次即显已备好（与 bind_special_to_lesson 同为材料位，两者并集推导）。

        🔴 只 UPDATE biz_course_plan_lesson.book_node_ids 单列——绝不整行 upsert（历史事故：
           整行重写把 paper_slots 已绑 paper_id 抹掉）。BE 端 partial updateById 只写 book_node_ids。

        参数:
          lesson_id: 课次 id（字符串）。
          node_id:   书章节节点 id（biz_shelf_node.id，字符串；action=materials 时忽略）。
          action:    'bind'（默认）/ 'unbind' / 'materials'（查本课已绑书章节概要）。
        返回:
          bind/unbind → {ok, lesson_id, book_node_ids:[...]}；
          materials   → {ok, lesson_id, book_node_ids:[...],
                         materials:[{nodeId,nodeTitle,bookId,bookTitle,questionCount}]}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        act = (action or "bind").strip().lower()
        try:
            if act == "materials":
                r = await client.teacher_get(f"{BASE}/lesson/{lesson_id}/book-materials")
                r = r or {}
                return {"ok": True, "lesson_id": str(lesson_id),
                        "book_node_ids": [str(x) for x in (r.get("bookNodeIds") or [])],
                        "materials": r.get("materials") or []}
            if act not in ("bind", "unbind"):
                return {"ok": False, "reason": f"未知 action: {action}（bind/unbind/materials）"}
            if not node_id:
                return {"ok": False, "reason": f"action={act} 需 node_id"}
            r = await client.teacher_post(f"{BASE}/lesson/{lesson_id}/{act}-node", {"nodeId": str(node_id)})
            r = r or {}
            return {"ok": True, "lesson_id": str(lesson_id),
                    "book_node_ids": [str(x) for x in (r.get("bookNodeIds") or [])]}
        except RuoyiError as e:
            return {"ok": False, "reason": f"{act} 失败: {e}"}

    @mcp.tool(tags={"shelf"})
    async def override_item(item_id: str, override: OverridePayload) -> dict:
        """书内改一道题的题面（D3 override 副本）：只影响本书，题库原子题**不动**。

        override.stem/options 写入 item.override_json；question_id 溯源保留（血缘不断）。
        还原原题 = 传空 override（{}）即清。含数学 <> 不会被 XSS 剥。
        返回 {ok}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        body = {"override": override.model_dump(exclude_none=True)}
        try:
            await client.teacher_put(f"{BASE}/item/{item_id}", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"override 失败: {e}"}
        return {"ok": True, "item_id": str(item_id)}
