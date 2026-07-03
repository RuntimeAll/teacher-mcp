# -*- coding: utf-8 -*-
"""docx → Tiptap 忠实还原转换器(不加 kgNote/kgCallout/kgMindmap/kgExample 等 LLM 加工节点)
纯标准节点:heading / paragraph(保留 bold/color marks) / table / image(上传OSS)
用法: python docx_to_tiptap_faithful.py <docx路径> <lesson_no> [apply]
产物: doc_json → 落库 biz_kg_doc(course_id=901001002, book_id=CC7S, lesson_no)
"""
import io, sys, os, json, re, tempfile, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
import pymysql

DOCX = sys.argv[1] if len(sys.argv) > 1 else r"D:\workplace\book-ai\预研空间\2025秋七上科学崔崔老师讲义\1.2.2科学测量——温度的测量（教师版）.docx"
LESSON_NO = int(sys.argv[2]) if len(sys.argv) > 2 else 2
APPLY = len(sys.argv) > 3 and sys.argv[3] == "apply"
BASE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(BASE, "_dbcfg.json"), encoding="utf-8"))
API = "http://localhost:8080"
CLIENT_ID = "e5cd7e4891bf95d1d19206ce24a7b32e"
COURSE_ID = "901001002"
BOOK_ID = "CC7S"
OSS_SUBDIR = "1.2.2"  # OSS 路径子目录标识

def http(path, payload, token=None):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("clientid", CLIENT_ID)
    if token: req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=60) as r:
        return json.loads(r.read().decode())

login = http("/auth/login", {"clientId": CLIENT_ID, "grantType": "password", "tenantId": "000000", "username": "teacher001", "password": "666666"})
TOKEN = login["data"]["access_token"]
print("[login] ok")

doc = Document(DOCX)

# ── body 顺序遍历(段落/表格交错) ──
def iter_block_items(parent):
    body = parent.element.body
    for child in body.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, parent)
        elif child.tag == qn('w:tbl'):
            yield Table(child, parent)

# ── 图片:提取 blob → 临时文件 → 上传 OSS ──
img_cache = {}  # rId → ossUrl
def upload_image(rid):
    if rid in img_cache:
        return img_cache[rid]
    part = doc.part.related_parts.get(rid)
    if part is None:
        return None
    blob = part.blob
    ext = (part.content_type.split("/")[-1] or "png").replace("jpeg", "jpg").replace("x-emf", "emf").replace("x-wmf", "wmf")
    if ext in ("emf", "wmf"):
        print(f"  [warn] {rid} 是 {ext} 矢量图,浏览器不支持,跳过(需转png)")
        img_cache[rid] = None
        return None
    fd, tmp = tempfile.mkstemp(suffix=f".{ext}", dir=BASE)
    os.write(fd, blob); os.close(fd)
    try:
        res = http("/teacher/ingest/image", {"localPath": os.path.abspath(tmp), "assetKind": f"kg_lecture_{OSS_SUBDIR}"}, TOKEN)
        resp = res.get("response") or res.get("data") or res  # /teacher/ 走 misikt envelope,取 response 层
        url = resp.get("ossUrl")
        img_cache[rid] = url
        print(f"  [img] {rid} → {url[-40:] if url else 'FAIL'}")
        return url
    finally:
        try: os.remove(tmp)
        except OSError: pass

EMU_PER_PX = 9525
MAX_W = 660  # 内容区宽度上限,超出 clamp

def para_images(p):
    """段落内图片: [(rid, width_px, height_px)],尺寸从 drawing extent(EMU)换算"""
    out = []
    for dr in p._p.findall('.//' + qn('w:drawing')):
        blip = dr.find('.//' + qn('a:blip'))
        rid = blip.get(qn('r:embed')) if blip is not None else None
        if not rid:
            continue
        ext = dr.find('.//' + qn('wp:extent'))
        w = h = None
        if ext is not None:
            try:
                w = round(int(ext.get('cx')) / EMU_PER_PX)
                h = round(int(ext.get('cy')) / EMU_PER_PX)
            except (TypeError, ValueError):
                w = h = None
        out.append((rid, w, h))
    return out

# ── run → Tiptap text node(保留 bold/italic/color) ──
def runs_to_text(p):
    nodes = []
    for run in p.runs:
        t = run.text
        if not t:
            continue
        marks = []
        if run.bold: marks.append({"type": "bold"})
        if run.italic: marks.append({"type": "italic"})
        col = None
        try:
            if run.font.color and run.font.color.rgb:
                col = "#" + str(run.font.color.rgb)
        except Exception:
            col = None
        if col and col.lower() not in ("#000000", "#null", "#none"):
            marks.append({"type": "textStyle", "attrs": {"color": col}})
        node = {"type": "text", "text": t}
        if marks: node["marks"] = marks
        nodes.append(node)
    # 合并相邻同 marks 的 text(可选,略)
    return nodes

# ── 段落样式 → heading level / paragraph ──
def para_style_level(p):
    name = (p.style.name if p.style else "") or ""
    if name.startswith("Heading"):
        try: return int(name.split()[-1])
        except ValueError: return 3
    # Normal 短句(<16字,非例题非答案)当子标题 heading4
    txt = p.text.strip()
    if name == "Normal" and 0 < len(txt) <= 15 and not re.match(r'^[（(【]?\d|^答案|^【|^[A-D][.．]', txt):
        return 4
    return None  # 普通段落

# ── 图片节点(内容区 clamp + 等比) ──
def image_node(rid, w, h):
    url = upload_image(rid)
    if not url:
        return None
    attrs = {"src": url}
    if w:
        cw = min(w, MAX_W)
        attrs["width"] = cw
        if h:
            attrs["height"] = round(h * cw / w)
    return {"type": "image", "attrs": attrs}

# ── 单元格内容:段落文字 + 内嵌图片(按文档顺序) ──
def cell_blocks(c):
    """把单元格内每个段落转成 paragraph 节点,段内图片作块级 image 紧随其后 —— 修复表格丢图误差"""
    blocks = []
    for cp in c.paragraphs:
        tn = runs_to_text(cp) or ([{"type": "text", "text": cp.text}] if cp.text.strip() else [])
        if tn:
            blocks.append({"type": "paragraph", "content": tn})
        for rid, w, h in para_images(cp):        # 单元格里的图片(温度计示意图等),原来被丢
            img = image_node(rid, w, h)
            if img:
                blocks.append(img)
    if not blocks:
        blocks = [{"type": "paragraph"}]
    return blocks

# ── 表格 → Tiptap table(单元格保留图片 + 从 docx 底纹同步背景色) ──
def cell_fill(c):
    """docx 单元格底纹填充色(w:shd@fill) → 十六进制;白/auto 视为默认不携带。"""
    tcpr = c._tc.find(qn('w:tcPr'))
    shd = tcpr.find(qn('w:shd')) if tcpr is not None else None
    fill = shd.get(qn('w:fill')) if shd is not None else None
    if fill and fill.upper() not in ("AUTO", "FFFFFF"):
        return "#" + fill.upper()
    return None

def table_to_node(tbl):
    """不强制 tableHeader(会给 Umo 默认灰底压住 docx 白字);背景走 attrs.background 从底纹同步,文字色由 runs_to_text 保留。"""
    rows = []
    for r in tbl.rows:
        cells = []
        for c in r.cells:
            cell = {"type": "tableCell", "content": cell_blocks(c)}
            bg = cell_fill(c)
            if bg:
                cell["attrs"] = {"background": bg}
            cells.append(cell)
        rows.append({"type": "tableRow", "content": cells})
    return {"type": "table", "content": rows}

# ── 主转换 ──
content = []
n_img = n_head = n_para = n_tbl = 0
for block in iter_block_items(doc):
    if isinstance(block, Table):
        content.append(table_to_node(block))
        n_tbl += 1
        continue
    p = block
    txt = p.text.strip()
    rids = para_images(p)
    # 先出文字(标题/段落)
    if txt:
        lvl = para_style_level(p)
        tnodes = runs_to_text(p) or [{"type": "text", "text": txt}]
        if lvl:
            content.append({"type": "heading", "attrs": {"level": min(lvl, 6)}, "content": tnodes})
            n_head += 1
        else:
            content.append({"type": "paragraph", "content": tnodes})
            n_para += 1
    # 再出图片(块级,顺序在段落之后),保留 docx 原始宽高(clamp 到内容区宽,等比)
    for rid, w, h in rids:
        img = image_node(rid, w, h)
        if img:
            content.append(img)
            n_img += 1

doc_json = {"type": "doc", "content": content}
title = "课时2 温度的测量"
print(f"\n[转换] heading={n_head} paragraph={n_para} table={n_tbl} image={n_img} 总块={len(content)}")
open(os.path.join(BASE, f"faithful_doc_lesson{LESSON_NO}.json"), "w", encoding="utf-8").write(json.dumps(doc_json, ensure_ascii=False, indent=1))
print(f"[产物] faithful_doc_lesson{LESSON_NO}.json")

if APPLY:
    conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], database=cfg["database"], charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SELECT id FROM biz_kg_doc WHERE course_id=%s AND book_id=%s AND lesson_no=%s", (COURSE_ID, BOOK_ID, LESSON_NO))
    row = cur.fetchone()
    dj = json.dumps(doc_json, ensure_ascii=False)
    if row:
        cur.execute("UPDATE biz_kg_doc SET doc_json=%s, title=%s WHERE id=%s", (dj, title, row[0]))
        print(f"[落库] 更新 doc id={row[0]}")
    else:
        cur.execute("INSERT INTO biz_kg_doc(course_id, book_id, lesson_no, title, doc_json, create_time) VALUES(%s,%s,%s,%s,%s,NOW())",
                    (COURSE_ID, BOOK_ID, LESSON_NO, title, dj))
        print(f"[落库] 新建 doc lesson_no={LESSON_NO}")
    conn.commit()
    conn.close()
else:
    print("(dry-run,未落库;加 apply 落库)")
