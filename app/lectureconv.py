# -*- coding: utf-8 -*-
"""讲义确定性转换（单一事实源，PRD-C-210）—— docx→Tiptap忠实内容 / 切片成挂KG片段 / 里外目录对齐闸。

从 PRD-C-207 scratchpad 三零件逐行上提（行为等价，溯源零件见 reference_207/）：
  · docx_to_tiptap_faithful.py → faithful_content()：段落样式→heading/paragraph(留 bold/italic/color mark)、
     表格 w:shd 底纹→背景色、单元格内图、EMF/WMF 矢量图跳过、图尺寸 EMU→px clamp。
  · v0_migrate.py           → split_frags()：H2 折进组首 H3、H3→知识点片段、普通节点归 cur_kp 或课时L4。
  · toc_compare.py          → toc_diff()：讲义 H3(里目录) ↔ KG 该课时知识点(外目录) 1:1 对齐裁决。

🔴 铁律（同 208 转换层）：零 LLM、零 login、零落库。图片不在此上传——出 〖图:rId〗 占位 + images 清单，
   由驱动 agent 走 upload_image 拿 ossUrl，再交 save_lecture_frag 回填（与 ingest 的 _imagify 同款拆分）。
"""
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

EMU_PER_PX = 9525
MAX_W = 660  # 内容区宽度上限，超出等比 clamp
IMG_PLACEHOLDER = "〖图:{rid}〗"  # 与 tools/ingest.py _imagify 同款占位约定


# ── body 顺序遍历（段落/表格交错） ──
def _iter_block_items(parent):
    body = parent.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


# ── 段落内图片: [(rid, width_px, height_px)]，尺寸从 drawing extent(EMU) 换算 ──
def _para_images(p):
    out = []
    for dr in p._p.findall(".//" + qn("w:drawing")):
        blip = dr.find(".//" + qn("a:blip"))
        rid = blip.get(qn("r:embed")) if blip is not None else None
        if not rid:
            continue
        ext = dr.find(".//" + qn("wp:extent"))
        w = h = None
        if ext is not None:
            try:
                w = round(int(ext.get("cx")) / EMU_PER_PX)
                h = round(int(ext.get("cy")) / EMU_PER_PX)
            except (TypeError, ValueError):
                w = h = None
        out.append((rid, w, h))
    return out


# ── run → Tiptap text node（保留 bold/italic/color mark）+ 行内 OMML→$LaTeX$ ──
_M_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


def _omml_latex(om):
    """OMML 元素 → $LaTeX$ 文本（复用 docconv 的 dwml 转换）。转不动时退纯文本，绝不静默丢。"""
    from app.docconv import _to_latex

    latex = (_to_latex(om) or "").strip()
    if latex and not latex.startswith("?"):
        return f"${latex}$"
    # dwml 转不动 → 至少把 m:t 文本捞出来（比丢了强）
    txt = "".join(t.text or "" for t in om.iter(_M_NS + "t"))
    return txt


def _run_node(run):
    t = run.text
    if not t:
        return None
    marks = []
    if run.bold:
        marks.append({"type": "bold"})
    if run.italic:
        marks.append({"type": "italic"})
    col = None
    try:
        if run.font.color and run.font.color.rgb:
            col = "#" + str(run.font.color.rgb)
    except Exception:
        col = None
    if col and col.lower() not in ("#000000", "#null", "#none"):
        marks.append({"type": "textStyle", "attrs": {"color": col}})
    node = {"type": "text", "text": t}
    if marks:
        node["marks"] = marks
    return node


def _runs_to_text(p):
    """按段落 XML 顺序出 text 节点：w:r 照常（marks），m:oMath/oMathPara → $LaTeX$ 文本。
    🔴 原版用 p.runs 会静默丢 OMML 公式（1.3 水火箭 ⅓ 教训，1.2.1/1.2.2 各丢 4/5 处）。"""
    from docx.text.run import Run

    nodes = []
    for child in p._p.iterchildren():
        tag = child.tag
        if tag == qn("w:r"):
            node = _run_node(Run(child, p))
            if node:
                nodes.append(node)
        elif tag in (_M_NS + "oMath", _M_NS + "oMathPara"):
            oms = [child] if tag == _M_NS + "oMath" else child.findall(_M_NS + "oMath")
            for om in oms:
                tx = _omml_latex(om)
                if tx:
                    nodes.append({"type": "text", "text": tx})
    return nodes


# ── 段落样式 → heading level / paragraph ──
def _para_style_level(p):
    name = (p.style.name if p.style else "") or ""
    if name.startswith("Heading"):
        try:
            return int(name.split()[-1])
        except ValueError:
            return 3
    # Normal 短句(<16字，非例题非答案)当子标题 heading4
    txt = p.text.strip()
    if name == "Normal" and 0 < len(txt) <= 15 and not re.match(r"^[（(【]?\d|^答案|^【|^[A-D][.．]", txt):
        return 4
    return None  # 普通段落


# ── 图片节点：出占位 src=〖图:rid〗 + 内容区 clamp 等比（上传由 agent 后置） ──
def _image_node(rid, w, h, images, inline=False):
    """images: dict 累加 {rid: None}（local_path 由 extract_images 另填）；节点 src 出占位待回填。
    inline=True → Umo inlineImage 节点（group=inline，横向流），用于「同段多图=一排」保留原版并排布局。"""
    images.setdefault(rid, None)
    attrs = {"src": IMG_PLACEHOLDER.format(rid=rid), "rid": rid}
    if w:
        cw = min(w, MAX_W)
        attrs["width"] = cw
        if h:
            attrs["height"] = round(h * cw / w)
    if inline:
        attrs["inline"] = True
        return {"type": "inlineImage", "attrs": attrs}
    return {"type": "image", "attrs": attrs}


def _emit_para_images(rids, images):
    """一个 docx 段落里的图 → 节点列表。
    ≥2 张 = 原版一排 → 合成一个 paragraph 内多个 inlineImage（横向流，保留并排布局）；
    单张 = block image（照旧竖直独占一行）。"""
    if len(rids) >= 2:
        kids = [_image_node(rid, w, h, images, inline=True) for rid, w, h in rids]
        return [{"type": "paragraph", "content": kids}]
    return [_image_node(rid, w, h, images) for rid, w, h in rids]


# ── 单元格内容：段落文字 + 内嵌图片（按文档顺序） ──
def _cell_blocks(c, images):
    blocks = []
    for cp in c.paragraphs:
        tn = _runs_to_text(cp) or ([{"type": "text", "text": cp.text}] if cp.text.strip() else [])
        if tn:
            blocks.append({"type": "paragraph", "content": tn})
        blocks.extend(_emit_para_images(_para_images(cp), images))
    if not blocks:
        blocks = [{"type": "paragraph"}]
    return blocks


# ── 单元格底纹填充色(w:shd@fill) → 十六进制；白/auto 视为默认不携带 ──
def _cell_fill(c):
    tcpr = c._tc.find(qn("w:tcPr"))
    shd = tcpr.find(qn("w:shd")) if tcpr is not None else None
    fill = shd.get(qn("w:fill")) if shd is not None else None
    if fill and fill.upper() not in ("AUTO", "FFFFFF"):
        return "#" + fill.upper()
    return None


def _table_to_node(tbl, images):
    rows = []
    for r in tbl.rows:
        cells = []
        for c in r.cells:
            cell = {"type": "tableCell", "content": _cell_blocks(c, images)}
            bg = _cell_fill(c)
            if bg:
                cell["attrs"] = {"background": bg}
            cells.append(cell)
        rows.append({"type": "tableRow", "content": cells})
    return {"type": "table", "content": rows}


def faithful_content(docx_path):
    """docx → (Tiptap content 列表, images 清单 {rid:None}, stats)。忠实转换，图出占位。

    行为等价于 207 docx_to_tiptap_faithful.py 的主循环（去掉内联上传，图 src=占位）。
    """
    doc = Document(docx_path)
    images = {}
    content = []
    n_img = n_head = n_para = n_tbl = 0
    for block in _iter_block_items(doc):
        if isinstance(block, Table):
            content.append(_table_to_node(block, images))
            n_tbl += 1
            continue
        p = block
        txt = p.text.strip()
        rids = _para_images(p)
        if txt:
            lvl = _para_style_level(p)
            tnodes = _runs_to_text(p) or [{"type": "text", "text": txt}]
            if lvl:
                content.append({"type": "heading", "attrs": {"level": min(lvl, 6)}, "content": tnodes})
                n_head += 1
            else:
                content.append({"type": "paragraph", "content": tnodes})
                n_para += 1
        for node in _emit_para_images(rids, images):
            content.append(node)
        n_img += len(rids)
    stats = {"heading": n_head, "paragraph": n_para, "table": n_tbl, "image": n_img, "blocks": len(content)}
    return content, images, stats


def extract_images(docx_path, out_dir, prefix, images):
    """把 images 清单里的 rid 抽成本地文件（复用 docconv.extract_docx_images），回填 local_path。

    返回 [{rid, local_path}]（EMF/WMF 等矢量图无对应媒体或浏览器不支持时 local_path 可能为 None）。
    """
    from app.docconv import extract_docx_images

    rid_to_path = extract_docx_images(docx_path, out_dir, prefix)
    out = []
    for rid in images:
        out.append({"rid": rid, "local_path": rid_to_path.get(rid)})
    return out


# ── 文本抽取（stem_text 镜像用；等价于 v0_migrate 的 txt()） ──
def node_text(node):
    if node is None:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(node_text(c) for c in (node.get("content", []) or []))


def render_with_markers(nodes):
    """把节点渲成**带结构标记的纯文本**（供解析 subagent 拆题）：
      · 图 → 〖图:rId〗 行内标记（与 208 约定一致，rid 对应 images 清单的本地图）
      · heading → 行首 [H{level}] 前缀（分组头/知识点组标题，给拆题当 kp 线索）
      · 段落/表格 → 逐块换行
    一块一行，保留文档顺序，让 LLM 看得清题界与图位。"""
    lines = []

    def render(node):
        t = node.get("type")
        if t == "image":
            rid = node.get("attrs", {}).get("rid")
            return f"〖图:{rid}〗" if rid else "〖图〗"
        if t == "text":
            return node.get("text", "")
        inner = "".join(render(c) for c in (node.get("content", []) or []))
        return inner

    for n in nodes:
        t = n.get("type")
        if t == "heading":
            lv = n.get("attrs", {}).get("level", 0)
            lines.append(f"[H{lv}] {render(n)}".rstrip())
        elif t == "table":
            # 表格 → markdown 表格语法（ingest/format 能渲染回真表格；格内图保留〖图:rId〗标记）。
            # 🔴 别打平成一行 [表] 文本——表格型题面(选项对照表等)会丢结构（1.2.3 题1 教训）。
            rows = []
            for r in n.get("content", []):
                cells = [(render(c).strip().replace("\n", " ") or " ") for c in r.get("content", [])]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                lines.append(rows[0])
                lines.append("|" + "---|" * max(rows[0].count("|") - 1, 1))
                lines.extend(rows[1:])
        else:
            line = render(n)
            if line.strip():
                lines.append(line)
    return "\n".join(lines)


def _heading_texts(nodes, level=3):
    out = []
    for n in nodes:
        if n.get("type") == "heading" and n.get("attrs", {}).get("level") == level:
            out.append(node_text(n).strip())
    return out


def split_frags(content, course_l4, kp_by_name):
    """把课时 content 切成挂 KG 节点的片段（等价 v0_migrate 切分段）。

    参数:
      content     : faithful_content 产出的 Tiptap 节点列表
      course_l4   : 课时 L4 subject_id（如 901001002001）——导语/未归属节点的落点
      kp_by_name  : {知识点名(strip): 知识点L5 subject_id}——H3 标题据此锚 KG
    返回:
      frags       : [{subject_id, kg_level, title, content_json_nodes, stem_text}]（按插入序）
      unmatched   : 未匹配到 KG 知识点的 H3 标题列表（对齐闸据此阻断）
      inner_h3    : 讲义里所有 H3 标题（里目录，供 toc_diff）
    """
    frags_nodes = {course_l4: []}
    order = [course_l4]
    cur_kp = None
    pending_h2 = None
    unmatched = []
    inner_h3 = []
    for n in content:
        t = n.get("type")
        lv = n.get("attrs", {}).get("level") if t == "heading" else None
        if t == "heading" and lv == 2:
            pending_h2 = n
            continue
        if t == "heading" and lv == 3:
            name = node_text(n).strip()
            inner_h3.append(name)
            kid = kp_by_name.get(name)
            if not kid:
                unmatched.append(name)
                kid = f"__UNMATCHED__{name}"
            cur_kp = kid
            if kid not in frags_nodes:
                frags_nodes[kid] = []
                order.append(kid)
            if pending_h2:
                frags_nodes[kid].append(pending_h2)
                pending_h2 = None
            frags_nodes[kid].append(n)
            continue
        target = course_l4 if cur_kp is None else cur_kp
        frags_nodes[target].append(n)
    if pending_h2:
        frags_nodes[course_l4].append(pending_h2)

    # 课时节点名 + 知识点名反查
    id_to_name = {v: k for k, v in kp_by_name.items()}
    frags = []
    for sid in order:
        nodes = frags_nodes[sid]
        lv = len(sid) // 3
        title = id_to_name.get(sid, sid) if sid != course_l4 else None  # 课时标题留空由 save 端取节点名
        stem = "".join(node_text(n) for n in nodes)
        frags.append({
            "subject_id": sid,
            "kg_level": lv,
            "title": title,
            "content_json_nodes": nodes,
            "stem_text": stem,
        })
    return frags, unmatched, inner_h3


def cuicui_split(content, course_l4, kp_by_name):
    """🔴 崔崔版式配方（确定性适配器，七上科学 30+ docx 通用）—— docx→10 知识点讲解片段。

    崔崔版式实测（1.2.1/1.2.2 等一致）：
      模块一：知识精讲 (H3)  ← 讲解主体，本适配器只切它
        ├─ 长度测量 / 体积测量 (H3 分组头，丢弃：知识点名已自带信息)
        │    └─ 测量的含义 / 长度的单位 … (H4，命中 KG 知识点名 → 一个片段)
      模块二：习题精练 (H3) / 模块三：巩固提升 (H3)  ← 全是题目，走题库(208)拿 qid，本适配器只返回其区间供后续处理

    配方规则：
      1. 定位「模块一知识精讲」区间（首个含「知识精讲」的 H3 → 下一个「模块二/习题/巩固」H3 或文末）。
      2. 区间内按「标题文本 == KG 知识点名（去空白）」切片；**匹配到的知识点标题提升为 H3**（与库内片段一致）。
      3. 分组头 H3（长度测量/体积测量）丢弃；知识点内的污染 H4（零刻度线：…）自然归入当前知识点。
    返回: (frags[{subject_id,kg_level,title,content_json_nodes,stem_text}] 按序, missing_kp_names, exercise_sections[{h3,start,end}])
          或 None（未识别出崔崔版式，调用侧应回退 assist 人工映射）。
    """
    def norm(s):
        return "".join(s.split())

    kp_norm = {norm(k): v for k, v in kp_by_name.items()}
    id2name = {v: k for k, v in kp_by_name.items()}

    # 1. 定位模块一区间 + 习题模块区间
    m1_start = None
    m1_end = len(content)
    exercise_sections = []
    for i, n in enumerate(content):
        if n.get("type") == "heading" and n.get("attrs", {}).get("level") == 3:
            t = norm(node_text(n))
            if m1_start is None and "知识精讲" in t:
                m1_start = i
            elif m1_start is not None and ("习题精练" in t or "巩固提升" in t or t.startswith("模块")):
                if m1_end == len(content):
                    m1_end = i
                exercise_sections.append({"h3": node_text(n).strip(), "start": i})
    if m1_start is None:
        return None  # 非崔崔版式 → 回退 assist

    # 习题区间收尾（每个习题模块 end = 下一个模块起点 or 文末）
    for k, sec in enumerate(exercise_sections):
        sec["end"] = exercise_sections[k + 1]["start"] if k + 1 < len(exercise_sections) else len(content)

    # 2. 模块一区间内按 H4 命中 KG 知识点名切片
    body = content[m1_start + 1:m1_end]
    frags_nodes = {}
    order = []
    cur = None
    for n in body:
        t = n.get("type")
        lv = n.get("attrs", {}).get("level") if t == "heading" else None
        txt = node_text(n).strip()
        if t == "heading" and lv == 3:
            continue  # 分组头（长度测量/体积测量），丢弃
        if t == "heading" and norm(txt) in kp_norm:
            cur = kp_norm[norm(txt)]
            if cur not in frags_nodes:
                frags_nodes[cur] = []
                order.append(cur)
            # 知识点标题提升为 H3（与库内其余片段一致）
            frags_nodes[cur].append({"type": "heading", "attrs": {"level": 3}, "content": n.get("content", [])})
            continue
        if cur is not None:
            frags_nodes[cur].append(n)
        # 知识点前的散块（模块一开头）丢弃：崔崔版式此处无内容

    frags = []
    for sid in order:
        nodes = frags_nodes[sid]
        frags.append({
            "subject_id": sid,
            "kg_level": len(sid) // 3,
            "title": id2name[sid],
            "content_json_nodes": nodes,
            "stem_text": "".join(node_text(x) for x in nodes),
        })
    missing = [id2name[v] for v in kp_by_name.values() if v not in frags_nodes]
    return frags, missing, exercise_sections


def sections_by_h3(content):
    """把忠实 content 按顶层 H3 切成可读段（供 agent 理解式映射时看清模块/分组边界）。

    返回 [{h3, start, end, node_count, preview}]；H3 前的散块归到一个 h3=None 的前言段。
    preview = 段内前若干块的纯文本拼接（截断），只为 agent 快速判断段性质（讲解/习题）。
    """
    sections = []
    cur = {"h3": None, "start": 0, "nodes": []}
    for i, n in enumerate(content):
        if n.get("type") == "heading" and n.get("attrs", {}).get("level") == 3:
            if cur["nodes"]:
                sections.append(cur)
            cur = {"h3": node_text(n).strip(), "start": i, "nodes": [n]}
        else:
            cur["nodes"].append(n)
    if cur["nodes"]:
        sections.append(cur)
    out = []
    for s in sections:
        preview = "".join(node_text(x) for x in s["nodes"])[:180]
        out.append({
            "h3": s["h3"],
            "start": s["start"],
            "end": s["start"] + len(s["nodes"]),
            "node_count": len(s["nodes"]),
            "preview": preview,
        })
    return out


def toc_diff(inner_h3, outer_kp_names):
    """里外目录对齐裁决（等价 toc_compare.py）：讲义 H3 vs KG 知识点，宽松去空白比对。

    返回: {matched, only_in_inner, missing, verdict:'PASS'|'FAIL'}。
    """
    def norm(s):
        return "".join(s.split())

    inner_set = {norm(x): x for x in inner_h3}
    outer_set = {norm(x): x for x in outer_kp_names}
    matched = [outer_set[k] for k in outer_set if k in inner_set]
    only_in = [inner_set[k] for k in inner_set if k not in outer_set]
    missing = [outer_set[k] for k in outer_set if k not in inner_set]
    verdict = "PASS" if not only_in and not missing else "FAIL"
    return {
        "matched": matched,
        "only_in_inner": only_in,
        "missing": missing,
        "verdict": verdict,
    }
