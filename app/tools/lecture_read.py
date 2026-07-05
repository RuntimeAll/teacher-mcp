"""MCP 工具·讲义读侧（PRD-C-213 R7a·据讲义出题）：list_lecture_docs / get_lecture_content。

🔴 全部打 **C 线 :8090**（挂 /teacher/kg/**，misikt envelope）——register 收 cluster，
   每个 @mcp.tool() 先 `client = await cluster.ensure_c()`（懒登录 C 线）。读端点在 BE 免登白名单，
   带 token 时按可见性（官方+本机构管理员+我的）返回。

🔴 讲义正文 = Tiptap doc JSON（heading/paragraph/table/image/**kgExample**）。三位一体铁律：
   例题只存 kgExample(qid) 引用、题面不落片段——本模块把 docJson 递归抽成纯文本 `text` 供 agent
   「读讲义」，例题位以 【例题 qid=...】 标记占位并把 qid 收集进 example_qids，题面内容让 agent 另调
   get_question(qid)。
"""
from typing import Optional

from app.ruoyi import RuoyiCluster, RuoyiError

BASE = "/teacher/kg"


def _extract_text(node, out: list, qids: list) -> None:
    """递归把 Tiptap 节点抽成纯文本行；kgExample→标记 + 收集 qid；image→【图】占位。"""
    if isinstance(node, list):
        for n in node:
            _extract_text(n, out, qids)
        return
    if not isinstance(node, dict):
        return
    ntype = node.get("type")
    if ntype == "text":
        t = node.get("text")
        if t:
            out.append(str(t))
        return
    if ntype == "kgExample":
        qid = (node.get("attrs") or {}).get("qid")
        if qid is not None:
            qid = str(qid)
            qids.append(qid)
            out.append(f"【例题 qid={qid}】")
        return
    if ntype in ("image", "inlineImage"):
        out.append("【图】")
        return
    # heading / paragraph / table 等容器：递归子节点，块级前后加换行分隔
    block = ntype in ("heading", "paragraph", "tableRow", "listItem", "blockquote")
    if block:
        out.append("\n")
    _extract_text(node.get("content"), out, qids)
    if block:
        out.append("\n")


def _doc_to_text(doc_json) -> tuple[str, list]:
    """Tiptap doc → (纯文本正文, example_qids 去重保序)。doc_json 为 None → ("", [])。"""
    if not doc_json:
        return "", []
    out: list = []
    qids: list = []
    _extract_text(doc_json.get("content") if isinstance(doc_json, dict) else doc_json, out, qids)
    text = "".join(out)
    # 折叠连续空行
    lines = [ln.rstrip() for ln in text.split("\n")]
    collapsed: list = []
    for ln in lines:
        if ln or (collapsed and collapsed[-1]):
            collapsed.append(ln)
    # 去重 qid 保序
    seen = set()
    uniq_qids = [q for q in qids if not (q in seen or seen.add(q))]
    return "\n".join(collapsed).strip(), uniq_qids


# ───────────────────────── 核心纯函数 ─────────────────────────
async def _list_lecture_docs(client, book_id="") -> dict:
    params: dict = {}
    if book_id:
        params["bookId"] = str(book_id)
    resp = await client.teacher_get(f"{BASE}/lecture-catalog", params)
    resp = resp or {}
    lessons = resp.get("lessons") if isinstance(resp, dict) else None
    return {
        "ok": True,
        "volume_id": (resp.get("volumeId") if isinstance(resp, dict) else None),
        "lessons": lessons or [],
    }


async def _get_lecture_content(client, subject_id, book_id="", owner=None) -> dict:
    params: dict = {"subjectId": str(subject_id)}
    if book_id:
        params["bookId"] = str(book_id)
    if owner is not None:
        params["owner"] = str(owner)
    resp = await client.teacher_get(f"{BASE}/lecture", params)
    resp = resp or {}
    doc_json = resp.get("docJson") if isinstance(resp, dict) else None
    text, example_qids = _doc_to_text(doc_json)
    return {
        "ok": True,
        "subject_id": str(subject_id),
        "node": (resp.get("node") if isinstance(resp, dict) else None),
        "book_id": (resp.get("bookId") if isinstance(resp, dict) else None),
        "owner": (resp.get("owner") if isinstance(resp, dict) else None),
        "has_content": bool(text),
        "text": text,
        "example_qids": example_qids,
    }


# ───────────────────────── MCP 工具注册（薄包一层）─────────────────────────
def register(mcp, cluster: RuoyiCluster) -> None:
    @mcp.tool()
    async def list_lecture_docs(book_id: str = "") -> dict:
        """查讲义目录（哪些课时/知识点有讲义片段，定位「据讲义出题」的锚点）→ C 线 :8090
        GET /teacher/kg/lecture-catalog。返回 {ok, volume_id, lessons}。

        参数:
          book_id : 教材/书 id（空=服务端默认书 DEFAULT_BOOK）。
        返回: {ok, volume_id, lessons:[...]}（lessons 为课时×来源聚合，含各课时 subjectId/标题/
              有无片段/owner 等；结构随 BE getCatalog 演进）。库里无讲义资产时 lessons=[]（空态非报错）。
        """
        try:
            client = await cluster.ensure_c()
            return await _list_lecture_docs(client, book_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def get_lecture_content(subject_id: str, book_id: str = "", owner: str = None) -> dict:
        """读某知识点/课次的讲义正文（🔴 据讲义出题的原料入口）→ C 线 :8090
        GET /teacher/kg/lecture?subjectId=。返回 {ok, subject_id, text, example_qids, ...}。

        🔴 text = docJson（Tiptap）递归抽出的纯讲解正文；例题只是 kgExample(qid) 引用——正文里以
           【例题 qid=...】 占位、真题面**不在**讲义片段里。要看例题题面 → 用返回的 example_qids
           调 get_question(qids)。据 text 理解知识点讲法后，agent 自己出同源题。
        参数:
          subject_id : 知识点/课次的 biz_subject id（get_plan_detail.kgNodeIds / resolve_kg 来）。
                       🔴 讲义按前缀树序汇聚：传课时节点会拿到其下片段拼成的整篇。
          book_id    : 教材/书 id（空=默认书）。
          owner      : 指定讲义作者 owner（字符串 userId）；空=默认视图（我的>本部门管理员>官方兜底）。
        返回: {ok, subject_id, node, book_id, owner, has_content, text, example_qids:[qid str]}。
              🔴 has_content=False / text="" = 该知识点无讲义资产（空态，agent 应降级为凭 KG + 题库出题）。
        """
        try:
            client = await cluster.ensure_c()
            return await _get_lecture_content(client, subject_id, book_id, owner)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
