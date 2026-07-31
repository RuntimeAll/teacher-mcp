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
        """新建一本空书（起步）。book_type 开放注册制（BE 不校验，任意 snake_case slug 可传）：
        已注册值 = lecture讲义 / workbook练习册 / textbook电子课本 / special备课挑题专项(不进书架列表) /
        variant_special举一反三专项(SOP-2 立项即建) / daily_punch每日打卡。
        新资料形态（口算集训、错题重练…）自定新 slug 即可，🔴 但须先到 认知/服务与能力总目录.md「书类型注册表」挂号 + book-ui BOOK_TYPE_LABEL 补中文名，否则前端显示裸 slug。

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
    async def set_punch_layout(book_id: str, show_info: Optional[bool] = None,
                               show_goals: Optional[bool] = None,
                               show_wrong_log: Optional[bool] = None,
                               reset: bool = False) -> dict:
        """打卡书**版面开关**：让固定区块显示/隐藏，**不动任何题目内容**。

        用途：同一套题换版面。比如「今日目标」对纯计算册是套话，关掉即可——
        不必逐天重灌 goals，也不必改主题模板。

        三个开关：
          show_info      班级/姓名/日期栏（题目卷顶部）
          show_goals     今日目标（题目卷）
          show_wrong_log 今日错题记录（解析卷末尾，家长核对栏）
        🔴 缺省全开，**只有显式传 False 才隐藏**；不传的键不动（老书零影响）。
        reset=True → 清空全部开关，整本恢复默认全开。

        展示页与导出 PDF 同源（都读 punch-v1 的 data.layout），改一次两边一起变。
        参数 book_id 字符串传。返回 {ok, book_id, punch_layout}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id).strip():
            return {"ok": False, "reason": "book_id 不能为空"}
        body: dict = {}
        if not reset:
            for key, val in (("showInfo", show_info), ("showGoals", show_goals),
                             ("showWrongLog", show_wrong_log)):
                if val is not None:
                    body[key] = bool(val)
            if not body:
                return {"ok": False, "reason": "至少给一个开关，或用 reset=True 恢复默认"}
        try:
            resp = await client.teacher_post(f"{BASE}/book/{book_id}/punch-layout", body)
        except RuoyiError as e:
            return {"ok": False, "reason": f"设置版面开关失败: {e}"}
        return {"ok": True, "book_id": str(book_id),
                "punch_layout": (resp or {}).get("punchLayout") if isinstance(resp, dict) else None}

    @mcp.tool(tags={"shelf"})
    async def save_book_recipe(book_id: str, recipe: dict) -> dict:
        """给书挂**生产配方溯源**（资料工厂 ↔ 线上书绑定，零 DDL 落 style_meta_json.recipe）。

        书是产物，recipe 记它**怎么造出来的**——下次要续造同类册子（下一期打卡、换年级、
        加天数），从线上书就能找回源头脚本，不必翻记忆或猜目录。造完一册就顺手挂上。

        约定键（软约定，结构不强校验，各产线形态差异大）：
          factory   主题/版式（如 'punch-v1'）
          engine    题目从哪来：'local-dsl'（自写表达式树）/ 'oralcalc-api'（出题器）/ 'manual'
          sourceDir 生产脚本目录（工作区相对路径）
          scripts   {角色: 文件名+一句话职责}
          rebuild   重跑命令（照抄能再造一册）
          seed      随机种子规则（可复现的关键）
          scale     {days, perDay, total}
          modules   模块组成与题量
          syllabus  教材进度红线（能出什么/不能出什么）
          gates     过了哪些闸（验算/查重/学段红线…）
          pdf       成品 PDF 落点
          builtAt / next  产出日期 / 续造要改哪几个参数
        🔴 只存**指针**不存正文（JSON ≤ 8000 字符），题库正文留在脚本里。
        只覆盖 recipe 键，netdisks/promo/accent 等其余键原样保留。
        参数 book_id 字符串传；recipe 传 {} = 清空。返回 {ok, book_id, recipe}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id).strip():
            return {"ok": False, "reason": "book_id 不能为空"}
        try:
            resp = await client.teacher_post(f"{BASE}/book/{book_id}/recipe", recipe or {})
        except RuoyiError as e:
            return {"ok": False, "reason": f"挂配方失败: {e}"}
        return {"ok": True, "book_id": str(book_id),
                "recipe": (resp or {}).get("recipe") if isinstance(resp, dict) else None}

    @mcp.tool(tags={"shelf"})
    async def set_book_public(book_id: str, public: bool = True) -> dict:
        """把书置为公开 / 取消公开（is_public 1/0）。🔴 **仅超级管理员可调**，老师调用 BE 返 403。

        「建书 ≠ 公开」：createBook 一律建成私有，公开是审定后的独立动作（与题目 set-public 同口径）。
        公开后该书对全体登录用户可读（书架列表 + 阅读页 + 导出），打卡书/电子课本上架前必走这一步。
        参数 book_id 字符串传（雪花号）。返回 {ok, book_id, public}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id).strip():
            return {"ok": False, "reason": "book_id 不能为空"}
        try:
            await client.teacher_put(f"{BASE}/book/{book_id}", {"isPublic": 1 if public else 0})
        except RuoyiError as e:
            return {"ok": False, "reason": f"置公开失败（非超管账号会 403）: {e}"}
        return {"ok": True, "book_id": str(book_id), "public": bool(public)}

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
