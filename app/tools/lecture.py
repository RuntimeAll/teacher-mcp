"""MCP 工具·讲义录入角色（PRD-C-210）：convert_lecture_docx（确定性转换预览） + save_lecture_frag（片段IR入库）。

🔴 姊妹于 208 题目录入，架构范式复刻，切在**片段 IR 收敛点**上做多源兼容：
   convert_lecture_docx = 崔崔 docx 适配器（确定性，零 login）→ 片段 IR 预览 + 里外目录对齐闸；
   save_lecture_frag    = 片段 IR → :8090（C 线 KgLectureController.saveFrags）唯一入库口，对来源无感。
🔴 多底座：save 打 **C 线 :8090**（讲义接口），例题录入打 A 线 :8080（走 208 ingest_items）——两套会话，
   本工具 register 传 _cluster，内部 await cluster.ensure_c() 懒登录 :8090。
🔴 图片同 208 拆分：convert 出 〖图:rId〗 占位 + images 清单；agent 走 upload_image 拿 ossUrl；
   save 传 image_map={rid:ossUrl} 回填。例题只存 kgExample(qid)（题面走 208 拿 qid，不落片段）。
"""
import json
from pathlib import Path

from app.ruoyi import RuoyiCluster, RuoyiError

ROOT = Path(__file__).resolve().parent.parent.parent
WORK = ROOT / ".lecture_work"
IMGDIR = ROOT / ".lecture_imgs"

UNMATCHED_PREFIX = "__UNMATCHED__"


def _drop_unresolved_images(content, image_map):
    """去掉无 ossUrl 的 image 节点（占位换不成 OSS 会渲染成裂图）+ 回填能换的 src。

    返回 (清洗后 content, unresolved rid 列表)。unresolved 不静默——save 返回里报告。
    """
    unresolved = []

    def scrub(nodes):
        out = []
        for node in nodes:
            if isinstance(node, dict):
                if node.get("type") == "image":
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


def register(mcp, cluster: RuoyiCluster) -> None:
    @mcp.tool()
    def convert_lecture_docx(docx_path: str, course_subject_id: str, book_id: str = "CC7S",
                             batch: str = "", mode: str = "assist") -> dict:
        """讲义 docx → 忠实 Tiptap 内容 + KG 知识点靶子 + 图清单（确定性，零 login，零落库）。理解式映射的原料台。

        🔴 崔崔版式实测=模块(H3)>分组(H3)>知识点(H4)，纯确定性 H3 切片对不上 KG 知识点 → 默认 mode='assist'：
           工具只做忠实转换 + 按顶层 H3 分段 + 给出该课时 KG 知识点靶子，**切成知识点由 agent 理解式映射**
           （读 raw_path 全文，按 kg_targets 重组讲解成片段 IR，习题模块的题走 208 拿 qid 后挂 kgExample）。
        mode='auto'：仅当来源 H3 已== KG 知识点名（已清洗源）才用——跑确定性 split_frags + 对齐闸，直接产 IR。
        🔴 图不在此上传：content 里 image.src=〖图:rId〗占位 + rid；images[].local_path 喂 upload_image 拿 ossUrl，
           save_lecture_frag(image_map={rid:ossUrl}) 回填。忠实转换细节：heading/paragraph 留 bold/italic/color、
           表 w:shd→背景色、单元格内图、图 EMU→px 内容区 clamp、EMF/WMF 矢量图无 local_path（浏览器不支持）。
        参数:
          docx_path         : 讲义 docx 绝对路径
          course_subject_id : 课时 L4 subject_id（如 901001002001）——KG 知识点靶子来源
          book_id           : 教辅套 id（崔崔=CC7S）
          batch             : 产物落盘名前缀（空=docx 文件名主干）
          mode              : 'assist'(默认，理解式原料) | 'auto'(确定性 H3 切片，仅清洗源可用)
        返回(assist): {ok, mode, course, kg_targets:[{id,name}], sections:[{h3,node_count,preview,start,end}],
                       images:[{rid,local_path}], raw_path, stats,
                       deterministic_hint:{toc,unmatched_h3,frags}}  ← 后者是 H3 切片试探，崔崔版必 FAIL 仅供参考。
        返回(auto):   {ok, mode, course, frag_count, frags, toc, unmatched_h3, images, ir_path, stats}
        """
        from app import db, lectureconv

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
                     "contentJson=Tiptap doc），习题模块的题走 208 ingest_items 拿 qid 后在对应片段插 "
                     "kgExample(qid) 节点，图先 upload_image；最后 save_lecture_frag。"),
        }

    @mcp.tool()
    async def save_lecture_frag(
        ir_path: str = "",
        frags: list = None,
        book_id: str = "CC7S",
        image_map: dict = None,
        owner: int = None,
        allow_toc_fail: bool = False,
    ) -> dict:
        """片段 IR → C 线 :8090 upsert 入库（唯一入库口，UK=subjectId+bookId+owner 幂等覆盖）。需先 login。

        🔴 打 C 线 :8090（cluster.ensure_c 懒登录），非 A 线；owner 省略=登录者名下（admin=uid1→官方讲义库）。
        图回填：image_map={rid:ossUrl} 把 content 里 〖图:rId〗占位换成 OSS 地址；无 ossUrl 的 image 节点会被剔除
           并在 unresolved_images 里报告（不静默丢）。对齐闸：任何 __UNMATCHED__ 片段默认拒绝入库（除非 allow_toc_fail）。
        参数:
          ir_path        : convert_lecture_docx 产出的 IR 文件路径（与 frags 二选一，优先）
          frags          : 片段 IR 列表 [{subjectId,title,contentJson,status?}]（与 ir_path 二选一）
          book_id        : 教辅套 id（ir_path 里带则以其为准）
          image_map      : {rId: ossUrl}（upload_image 的产物）
          owner          : 归属 uid；省略=登录者（admin 登录省略即官方库覆盖）
          allow_toc_fail : True 才允许含未匹配 KG 的片段入库（BE 仍会因 subjectId 不存在而单条失败）
        返回: BE saveFrags 响应 {ok, owner, results:[{subjectId,action}], stats} + {unresolved_images}。
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
            c = await cluster.ensure_c()
            resp = await c.teacher_post("/teacher/kg/lecture-frag", body)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        if isinstance(resp, dict):
            resp["unresolved_images"] = sorted(set(unresolved_all))
        return resp

    @mcp.tool()
    async def remove_lecture_frag(subject_prefix: str, book_id: str = "CC7S", owner: int = None) -> dict:
        """删除某 owner 在某 subjectId 前缀下的讲义片段（覆盖录入的「先删」步；打 C 线 :8090）。需先 login。

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
            c = await cluster.ensure_c()
            if body["owner"] is None:
                body["owner"] = c.user_id  # BE remove 要求 owner 必填；省略=登录者
            resp = await c.teacher_post("/teacher/kg/lecture-frag/remove", body)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        return resp


def _iter_type(nodes, t):
    for n in nodes:
        if isinstance(n, dict):
            if n.get("type") == t:
                yield n
            yield from _iter_type(n.get("content", []) or [], t)
