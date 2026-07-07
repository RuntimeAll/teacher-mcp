"""MCP 工具·讲义录入组（data 角色）：
  写层  convert_lecture_docx / save_lecture_frag / remove_lecture_frag（PRD-C-210）
  读层  list_lecture_docs / get_lecture_content（PRD-C-213 R7a·据讲义出题）

PRD-O-005 重建：合并旧 app/tools/{lecture,lecture_read}.py；单 client（:9090，A/C 已合并），
删 cluster.ensure_c()，工具直接用注入的 client（讲义正文 = Tiptap doc JSON）。
"""
import json
from pathlib import Path
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

# 仓根 = src/teacher_mcp/tools/data_lecture.py 往上四层
ROOT = Path(__file__).resolve().parent.parent.parent.parent
WORK = ROOT / ".lecture_work"
IMGDIR = ROOT / ".lecture_imgs"

BASE = "/teacher/kg"
UNMATCHED_PREFIX = "__UNMATCHED__"


# ───────────────────────── 讲义写侧辅助 ─────────────────────────
def _drop_unresolved_images(content, image_map):
    """去掉无 ossUrl 的 image 节点（占位换不成 OSS 会渲染成裂图）+ 回填能换的 src。

    返回 (清洗后 content, unresolved rid 列表)。unresolved 不静默——save 返回里报告。
    """
    unresolved = []

    def scrub(nodes):
        out = []
        for node in nodes:
            if isinstance(node, dict):
                if node.get("type") in ("image", "inlineImage"):
                    attrs = node.get("attrs", {})
                    rid = attrs.get("rid")
                    url = (image_map or {}).get(rid) if rid else None
                    if rid and not url:
                        unresolved.append(rid)
                        continue  # 剔除裂图节点
                    if url:
                        attrs["src"] = url
                    attrs.pop("rid", None)
                    node["attrs"] = attrs
                inner = node.get("content")
                if isinstance(inner, list):
                    node["content"] = scrub(inner)
            out.append(node)
        return out

    kept = scrub(content)
    return kept, unresolved


def _build_ir(frags, course_subject_id, course_kg_name):
    """确定性 split_frags 产物 → (IR frags, summary)。仅 mode='auto' 用。"""
    ir_frags, summary = [], []
    for f in frags:
        nodes = f["content_json_nodes"]
        title = f["title"] or (course_kg_name if f["subject_id"] == course_subject_id else f["subject_id"])
        ir_frags.append({
            "subjectId": f["subject_id"], "kg_level": f["kg_level"], "title": title,
            "contentJson": {"type": "doc", "content": nodes}, "stem_text": f["stem_text"],
        })
        summary.append({
            "subjectId": f["subject_id"], "title": title, "kg_level": f["kg_level"],
            "nodes": len(nodes), "images": sum(1 for _ in _iter_type(nodes, "image")),
            "examples": sum(1 for _ in _iter_type(nodes, "kgExample")),
            "unmatched": f["subject_id"].startswith(UNMATCHED_PREFIX),
        })
    return ir_frags, summary


def _iter_type(nodes, t):
    for n in nodes:
        if isinstance(n, dict):
            if n.get("type") == t:
                yield n
            yield from _iter_type(n.get("content", []) or [], t)


# ───────────────────────── 讲义读侧辅助 ─────────────────────────
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


def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool(tags={"data", "lecture"})
    def convert_lecture_docx(docx_path: str, course_subject_id: str, book_id: str = "CC7S",
                             batch: str = "", mode: str = "assist") -> dict:
        """讲义 docx → 忠实 Tiptap 内容 + KG 知识点靶子 + 图清单（确定性，零 login，零落库）。理解式映射的原料台。

        🔴 崔崔版式实测=模块(H3)>分组(H3)>知识点(H4)，纯确定性 H3 切片对不上 KG 知识点 → 默认 mode='assist'：
           工具只做忠实转换 + 按顶层 H3 分段 + 给出该课时 KG 知识点靶子，**切成知识点由 agent 理解式映射**
           （读 raw_path 全文，按 kg_targets 重组讲解成片段 IR，习题模块的题走 ingest_items 拿 qid 后挂 kgExample）。
        mode='auto'：仅当来源 H3 已== KG 知识点名（已清洗源）才用——跑确定性 split_frags + 对齐闸，直接产 IR。
        🔴 图不在此上传：content 里 image.src=〖图:rId〗占位 + rid；images[].local_path 喂 upload_image 拿 ossUrl，
           save_lecture_frag(image_map={rid:ossUrl}) 回填。忠实转换细节：heading/paragraph 留 bold/italic/color、
           表 w:shd→背景色、单元格内图、图 EMU→px 内容区 clamp、EMF/WMF 矢量图无 local_path（浏览器不支持）。
        参数:
          docx_path         : 讲义 docx 绝对路径
          course_subject_id : 课时 L4 subject_id（如 901001002001）——KG 知识点靶子来源
          book_id           : 教辅套 id（崔崔=CC7S）
          batch             : 产物落盘名前缀（空=docx 文件名主干）
          mode              : 'assist'(默认，理解式原料) | 'auto'(确定性 H3 切片，仅清洗源可用) | 'cuicui'(崔崔适配器)
        返回(assist): {ok, mode, course, kg_targets:[{id,name}], sections:[{h3,node_count,preview,start,end}],
                       images:[{rid,local_path}], raw_path, stats,
                       deterministic_hint:{toc,unmatched_h3,frags}}  ← 后者是 H3 切片试探，崔崔版必 FAIL 仅供参考。
        返回(auto):   {ok, mode, course, frag_count, frags, toc, unmatched_h3, images, ir_path, stats}
        """
        from teacher_mcp.backends import db
        from teacher_mcp.domains import lectureconv

        p = Path(docx_path)
        if not p.exists():
            return {"ok": False, "reason": f"文件不存在: {docx_path}"}
        if not course_subject_id:
            return {"ok": False, "reason": "course_subject_id 必填（课时 L4，如 901001002001）"}
        name = batch or p.stem.replace("#", "_").replace(" ", "_")

        # KG 知识点靶子：该课时直接子知识点
        try:
            course_nodes = db.kg_query("", parent_id=course_subject_id, limit=200)
        except Exception as e:
            return {"ok": False, "reason": f"KG 查表失败: {type(e).__name__}: {e}"}
        if not course_nodes:
            return {"ok": False, "reason": f"course_subject_id 无子知识点或不存在: {course_subject_id}"}
        kp_by_name = {n["name"].strip(): n["id"] for n in course_nodes}
        kg_targets = [{"id": n["id"], "name": n["name"].strip()} for n in course_nodes]
        outer_names = [t["name"] for t in kg_targets]

        # 忠实转换 + 抽图
        try:
            content, images_dict, stats = lectureconv.faithful_content(str(p))
        except Exception as e:
            return {"ok": False, "reason": f"docx 转换失败: {type(e).__name__}: {e}"}
        images = lectureconv.extract_images(str(p), IMGDIR, name, images_dict)

        # 课时自身名
        course_kg_name = None
        try:
            self_rows = db.kg_query(course_subject_id, query="", limit=300)
            course_kg_name = next((r["name"] for r in self_rows if r["id"] == course_subject_id), None)
        except Exception:
            pass
        course = {"subject_id": course_subject_id, "name": course_kg_name, "kp_count": len(kg_targets)}

        WORK.mkdir(exist_ok=True)

        if mode == "cuicui":
            # 🔴 崔崔版式确定性适配器（七上科学 30+ docx 通用配方，见 lectureconv.cuicui_split）
            res = lectureconv.cuicui_split(content, course_subject_id, kp_by_name)
            if res is None:
                return {"ok": False, "reason": "未识别出崔崔「模块一知识精讲」结构；改用 mode='assist' 人工映射",
                        "hint_sections": lectureconv.sections_by_h3(content)}
            frags, missing, exercise_sections = res
            ir_frags, summary = _build_ir(frags, course_subject_id, course_kg_name)
            ir_path = WORK / f"{name}_ir.json"
            ir_path.write_text(json.dumps({"book_id": book_id, "course_subject_id": course_subject_id,
                                           "frags": ir_frags}, ensure_ascii=False, indent=1), encoding="utf-8")
            # 习题原始文本（模块二/三，带 〖图:rId〗+[H] 标记）→ 交解析 subagent 拆题绑点
            ex_raw_path = None
            if exercise_sections:
                ex_start = exercise_sections[0]["start"]
                ex_end = exercise_sections[-1]["end"]
                ex_text = lectureconv.render_with_markers(content[ex_start:ex_end])
                ex_raw_path = WORK / f"{name}_exercises_raw.txt"
                ex_raw_path.write_text(ex_text, encoding="utf-8")
            coverage_ok = not missing
            return {"ok": True, "mode": "cuicui", "course": course, "frag_count": len(ir_frags),
                    "frags": summary, "coverage": {"kp_total": len(kg_targets), "covered": len(ir_frags),
                                                   "missing": missing, "verdict": "PASS" if coverage_ok else "FAIL"},
                    "exercise_sections": exercise_sections, "exercise_raw_path": str(ex_raw_path) if ex_raw_path else None,
                    "kg_targets": kg_targets, "images": images, "ir_path": str(ir_path), "stats": stats,
                    "note": ("覆盖闸 PASS：讲解 10 片段就绪，上传图后 save_lecture_frag；习题=exercise_raw_path 交解析 subagent 拆题绑点(kg_targets)→bulk ingest_items→qid→(可选)挂 kgExample"
                             if coverage_ok else f"🔴 覆盖闸 FAIL：知识点 {missing} 无讲解片段，人工核对（版式可能偏离崔崔标准）")}

        if mode == "auto":
            frags, unmatched, inner_h3 = lectureconv.split_frags(content, course_subject_id, kp_by_name)
            toc = lectureconv.toc_diff(inner_h3, outer_names)
            ir_frags, summary = _build_ir(frags, course_subject_id, course_kg_name)
            ir_path = WORK / f"{name}_ir.json"
            ir_path.write_text(json.dumps({"book_id": book_id, "course_subject_id": course_subject_id,
                                           "frags": ir_frags}, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True, "mode": "auto", "course": course, "frag_count": len(ir_frags),
                    "frags": summary, "toc": toc, "unmatched_h3": unmatched, "images": images,
                    "ir_path": str(ir_path), "stats": stats,
                    "note": ("对齐闸 PASS，上传图后 save_lecture_frag" if toc["verdict"] == "PASS"
                             else "🔴 对齐闸 FAIL：H3 非知识点结构，改用 mode='assist' 理解式映射")}

        # mode == 'assist'：出原料，切成知识点交给 agent
        raw_path = WORK / f"{name}_raw.json"
        raw_path.write_text(json.dumps({"type": "doc", "content": content}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        sections = lectureconv.sections_by_h3(content)
        # 确定性切片试探（仅参考，崔崔版必 FAIL）
        frags, unmatched, inner_h3 = lectureconv.split_frags(content, course_subject_id, kp_by_name)
        toc = lectureconv.toc_diff(inner_h3, outer_names)
        return {
            "ok": True, "mode": "assist", "course": course, "kg_targets": kg_targets,
            "sections": sections, "images": images, "raw_path": str(raw_path), "stats": stats,
            "deterministic_hint": {"toc": toc, "unmatched_h3": unmatched,
                                   "inner_h3": inner_h3},
            "note": ("读 raw_path 全文，按 kg_targets 把讲解重组成片段 IR（每片段 subjectId=知识点id、"
                     "contentJson=Tiptap doc），习题模块的题走 ingest_items 拿 qid 后在对应片段插 "
                     "kgExample(qid) 节点，图先 upload_image；最后 save_lecture_frag。"),
        }

    @mcp.tool(tags={"data", "lecture"})
    async def save_lecture_frag(
        ir_path: str = "",
        frags: list = None,
        book_id: str = "CC7S",
        image_map: dict = None,
        owner: int = None,
        allow_toc_fail: bool = False,
    ) -> dict:
        """片段 IR → :9090 upsert 入库（唯一入库口，UK=subjectId+bookId+owner 幂等覆盖）。需先 login。

        图回填：image_map={rid:ossUrl} 把 content 里 〖图:rId〗占位换成 OSS 地址；无 ossUrl 的 image 节点会被剔除
           并在 unresolved_images 里报告（不静默丢）。对齐闸：任何 __UNMATCHED__ 片段默认拒绝入库（除非 allow_toc_fail）。
        参数:
          ir_path        : convert_lecture_docx 产出的 IR 文件路径（与 frags 二选一，优先）
          frags          : 片段 IR 列表 [{subjectId,title,contentJson,status?}]（与 ir_path 二选一）
          book_id        : 教辅套 id（ir_path 里带则以其为准）
          image_map      : {rId: ossUrl}（upload_image 的产物）
          owner          : 归属 uid；省略=登录者（admin 登录省略即官方库覆盖）
          allow_toc_fail : True 才允许含未匹配 KG 的片段入库（BE 仍会因 subjectId 不存在而单条失败）
        返回: BE saveFrags 响应 {ok, owner, results:[{subjectId,action}], stats} + {unresolved_images, view_url?}。
        """
        if ir_path and not frags:
            fp = Path(ir_path)
            if not fp.exists():
                return {"ok": False, "reason": f"ir_path 不存在: {ir_path}"}
            payload = json.loads(fp.read_text(encoding="utf-8"))
            frags = payload.get("frags") or []
            book_id = payload.get("book_id") or book_id
        if not frags:
            return {"ok": False, "reason": "ir_path 或 frags 至少给一个（且非空）"}

        # 对齐闸：未匹配片段拦截
        unmatched = [f.get("subjectId") for f in frags if str(f.get("subjectId", "")).startswith(UNMATCHED_PREFIX)]
        if unmatched and not allow_toc_fail:
            return {"ok": False, "reason": f"对齐闸拦截：{len(unmatched)} 个未匹配 KG 的片段 {unmatched[:5]}；"
                                           f"修正目录或传 allow_toc_fail=True 强过", "unmatched": unmatched}

        # 图回填 + 剔除未解析图
        unresolved_all = []
        body_frags = []
        for f in frags:
            if str(f.get("subjectId", "")).startswith(UNMATCHED_PREFIX):
                continue  # 已拦截；allow_toc_fail 时也不送未匹配 sid（BE 必拒）
            cj = f.get("contentJson")
            if isinstance(cj, str):
                cj = json.loads(cj)
            content = (cj or {}).get("content", [])
            content, unresolved = _drop_unresolved_images(content, image_map)
            unresolved_all += unresolved
            body_frags.append({
                "subjectId": f.get("subjectId"),
                "title": f.get("title"),
                "contentJson": {"type": "doc", "content": content},
                "status": f.get("status", "0"),
            })

        body = {"bookId": book_id, "frags": body_frags}
        if owner is not None:
            body["owner"] = owner
        try:
            resp = await client.teacher_post("/teacher/kg/lecture-frag", body)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        if isinstance(resp, dict):
            resp["unresolved_images"] = sorted(set(unresolved_all))
            # 🔴 写工具返回 book-ui 深链（讲义浏览页）
            resp["view_url"] = "http://localhost:9091/lecture-hub"
        return resp

    @mcp.tool(tags={"data", "lecture"})
    async def remove_lecture_frag(subject_prefix: str, book_id: str = "CC7S", owner: int = None) -> dict:
        """删除某 owner 在某 subjectId 前缀下的讲义片段（覆盖录入的「先删」步；打 :9090）。需先 login。

        🔴 前缀 LIKE 删除：`subject_prefix='901001002001'`(课时L4) 会删该课时**自身 + 全部子知识点**片段——
           连课时级思维导图(kgMindmap)一并删，想保留导图就别用课时前缀，改删到知识点段（或逐个知识点前缀）。
           BE 强制 subjectPrefix≥9 位(节级)防误删整册。owner 省略=登录者（admin=uid1 官方库）。
        参数:
          subject_prefix : subjectId 前缀（≥9 位）；删该前缀下该 owner 的所有片段
          book_id        : 教辅套 id
          owner          : 归属 uid；省略=登录者
        返回: BE {ok, removed}（removed=删除行数）。
        """
        if not subject_prefix or len(subject_prefix) < 9:
            return {"ok": False, "reason": "subject_prefix 至少 9 位（节级），防误删整册"}
        body = {"bookId": book_id, "subjectPrefix": subject_prefix}
        body["owner"] = owner if owner is not None else None
        try:
            if body["owner"] is None:
                body["owner"] = client.user_id  # BE remove 要求 owner 必填；省略=登录者
            resp = await client.teacher_post("/teacher/kg/lecture-frag/remove", body)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        return resp

    @mcp.tool(tags={"data", "lecture", "prep"})
    async def list_lecture_docs(book_id: str = "") -> dict:
        """查讲义目录（哪些课时/知识点有讲义片段，定位「据讲义出题」的锚点）→ :9090
        GET /teacher/kg/lecture-catalog。返回 {ok, volume_id, lessons}。

        参数:
          book_id : 教材/书 id（空=服务端默认书 DEFAULT_BOOK）。
        返回: {ok, volume_id, lessons:[...]}（lessons 为课时×来源聚合，含各课时 subjectId/标题/
              有无片段/owner 等；结构随 BE getCatalog 演进）。库里无讲义资产时 lessons=[]（空态非报错）。
        """
        try:
            return await _list_lecture_docs(client, book_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"data", "lecture", "prep"})
    async def get_lecture_content(subject_id: str, book_id: str = "", owner: str = None) -> dict:
        """读某知识点/课次的讲义正文（🔴 据讲义出题的原料入口）→ :9090
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
            return await _get_lecture_content(client, subject_id, book_id, owner)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
