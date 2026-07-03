# -*- coding: utf-8 -*-
"""1.2.2 课时终版组装 = 对齐 1.2.1 范本：
  kgMindmap(交互思维导图) + h2 专题组 + h3=KG 7 知识点(里外对齐) + 忠实内容(段落/表格含图/图片) + kgExample(qid 内联)
忠实还原:名师解读/说明 → 普通段落(不用插件);内容忠于 docx。
用法: python build_lesson2_final.py [apply]
"""
import io, sys, os, json, tempfile, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
import pymysql

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"
BASE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(BASE, "_dbcfg.json"), encoding="utf-8"))
qmap = json.load(open(os.path.join(BASE, "ex_qid_map.json"), encoding="utf-8"))
API = "http://localhost:8080"
CLIENT_ID = "e5cd7e4891bf95d1d19206ce24a7b32e"
COURSE_ID = "901001002"; BOOK_ID = "CC7S"; LESSON_NO = 2
DOCX = r"D:\workplace\book-ai\预研空间\2025秋七上科学崔崔老师讲义\1.2.2科学测量——温度的测量（教师版）.docx"
MAX_W = 660; EMU = 9525
KP = {"概念":"901001002002001","摄氏":"901001002002002","原理构造":"901001002002003",
      "种类":"901001002002004","使用":"901001002002005","体温计":"901001002002006","换算":"901001002002007"}

def http(path, payload, token=None):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json"); req.add_header("clientid", CLIENT_ID)
    if token: req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=60) as r: return json.loads(r.read().decode())
login = http("/auth/login", {"clientId": CLIENT_ID, "grantType": "password", "tenantId": "000000", "username": "teacher001", "password": "666666"})
TOKEN = login["data"]["access_token"]

doc = Document(DOCX)
def iter_blocks(parent):
    for child in parent.element.body.iterchildren():
        if child.tag == qn('w:p'): yield Paragraph(child, parent)
        elif child.tag == qn('w:tbl'): yield Table(child, parent)
BLOCKS = list(iter_blocks(doc))

img_cache = {}
def upload_image(rid):
    if rid in img_cache: return img_cache[rid]
    part = doc.part.related_parts.get(rid)
    if part is None: return None
    ext = part.content_type.split("/")[-1].replace("jpeg", "jpg")
    if ext in ("x-emf", "x-wmf", "emf", "wmf"): img_cache[rid] = None; return None
    fd, tmp = tempfile.mkstemp(suffix="." + ext, dir=BASE); os.write(fd, part.blob); os.close(fd)
    try:
        r = http("/teacher/ingest/image", {"localPath": os.path.abspath(tmp), "assetKind": "kg_lecture_1.2.2"}, TOKEN)
        url = (r.get("response") or r.get("data") or {}).get("ossUrl"); img_cache[rid] = url; return url
    finally:
        try: os.remove(tmp)
        except OSError: pass

def runs_to_text(p):
    nodes = []
    for run in p.runs:
        t = run.text
        if not t: continue
        marks = []
        if run.bold: marks.append({"type": "bold"})
        if run.italic: marks.append({"type": "italic"})
        try:
            col = "#" + str(run.font.color.rgb) if (run.font.color and run.font.color.rgb) else None
        except Exception: col = None
        if col and col.lower() not in ("#000000", "#null", "#none"):
            marks.append({"type": "textStyle", "attrs": {"color": col}})
        n = {"type": "text", "text": t}
        if marks: n["marks"] = marks
        nodes.append(n)
    return nodes

def para_imgs(p):
    out = []
    for dr in p._p.findall('.//' + qn('w:drawing')):
        blip = dr.find('.//' + qn('a:blip')); rid = blip.get(qn('r:embed')) if blip is not None else None
        if not rid: continue
        ext = dr.find('.//' + qn('wp:extent')); w = h = None
        if ext is not None:
            try: w = round(int(ext.get('cx'))/EMU); h = round(int(ext.get('cy'))/EMU)
            except (TypeError, ValueError): w = h = None
        out.append((rid, w, h))
    return out

def img_node(rid, w, h):
    url = upload_image(rid)
    if not url: return None
    a = {"src": url}
    if w:
        cw = min(w, MAX_W); a["width"] = cw
        if h: a["height"] = round(h*cw/w)
    return {"type": "image", "attrs": a}

def cell_blocks(c):
    bl = []
    for cp in c.paragraphs:
        tn = runs_to_text(cp) or ([{"type": "text", "text": cp.text}] if cp.text.strip() else [])
        if tn: bl.append({"type": "paragraph", "content": tn})
        for rid, w, h in para_imgs(cp):
            im = img_node(rid, w, h)
            if im: bl.append(im)
    return bl or [{"type": "paragraph"}]

def cell_fill(c):
    """docx 单元格底纹填充色(w:shd@fill) → 十六进制;白/auto 视为默认不携带。"""
    tcpr = c._tc.find(qn('w:tcPr'))
    shd = tcpr.find(qn('w:shd')) if tcpr is not None else None
    fill = shd.get(qn('w:fill')) if shd is not None else None
    if fill and fill.upper() not in ("AUTO", "FFFFFF"):
        return "#" + fill.upper()
    return None

def table_node(tbl):
    """不强制 tableHeader;单元格背景从 docx 底纹同步(attrs.background),文字色由 runs_to_text 保留 → 橙底白字忠实还原。"""
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

# ── 把 docx 块 i 转成节点列表(段落+图片);table 单独处理 ──
def block_nodes(i, bold=False):
    b = BLOCKS[i]
    if isinstance(b, Table): return [table_node(b)]
    out = []
    tn = runs_to_text(b)
    if tn:
        if bold:
            for n in tn: n.setdefault("marks", []).append({"type": "bold"})
        out.append({"type": "paragraph", "content": tn})
    for rid, w, h in para_imgs(b):
        im = img_node(rid, w, h)
        if im: out.append(im)
    return out

def h(level, text): return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}
def para(text): return {"type": "paragraph", "content": [{"type": "text", "text": text}]}
def example(key, kp): return {"type": "kgExample", "attrs": {"qid": qmap[key], "knowledgeId": kp}}

# 全部 20 道例题按知识点分配(混排进各知识点段落末尾)
EX_BY_KP = {
  "概念": ["CC7S-1.2.2-m3_1", "CC7S-1.2.2-m3_6"],
  "原理构造": ["CC7S-1.2.2-m3_10"],
  "使用": ["CC7S-1.2.2-ex1", "CC7S-1.2.2-ex2", "CC7S-1.2.2-ex3", "CC7S-1.2.2-m3_2", "CC7S-1.2.2-m3_3", "CC7S-1.2.2-m3_9", "CC7S-1.2.2-m3_11"],
  "换算": ["CC7S-1.2.2-ex7", "CC7S-1.2.2-m2_5", "CC7S-1.2.2-m3_4"],
  "体温计": ["CC7S-1.2.2-ex4", "CC7S-1.2.2-ex5", "CC7S-1.2.2-ex6", "CC7S-1.2.2-m2_4", "CC7S-1.2.2-m3_5", "CC7S-1.2.2-m3_7", "CC7S-1.2.2-m3_8"],
}
def add_examples(kpname):
    kp = KP[kpname]
    return [{"type": "kgExample", "attrs": {"qid": qmap[k], "knowledgeId": kp}} for k in EX_BY_KP.get(kpname, [])]

# ── 思维导图树(交互版,替代 docx png) ──
MIND = {"text": "温度的测量", "children": [
  {"text": "温度", "color": "#0ea5e9", "children": [
    {"text": "概念", "detail": "表示物体的冷热程度"},
    {"text": "单位·摄氏度℃", "detail": "冰水混合物0℃ / 沸水100℃"}]},
  {"text": "温度计", "color": "#22c55e", "children": [
    {"text": "原理", "detail": "液体热胀冷缩"},
    {"text": "构造", "detail": "玻璃泡+细管+刻度"},
    {"text": "种类", "detail": "酒精/水银/煤油"}]},
  {"text": "使用方法", "color": "#f59e0b", "children": [
    {"text": "选对", "detail": "量程合适"}, {"text": "放对", "detail": "浸没·不碰壁底"},
    {"text": "读对", "detail": "稳定后·视线平"}, {"text": "记对", "detail": "数值+单位+负号"}]},
  {"text": "体温计", "color": "#ef4444", "children": [
    {"text": "规格", "detail": "量程35~42℃ 分度0.1℃"},
    {"text": "缩口", "detail": "离体不回落·用前需甩"}]},
]}

content = []
content.append({"type": "kgMindmap", "attrs": {"data": json.dumps(MIND, ensure_ascii=False)}})

# ═══ h2 温度与温度计 ═══
content.append(h(2, "温度与温度计"))
content.append(h(3, "温度的概念")); content += block_nodes(6) + add_examples("概念")
content.append(h(3, "摄氏温度及其单位")); content += block_nodes(7, bold=True) + block_nodes(8) + block_nodes(9)
content.append(h(3, "温度计的原理与构造")); content += block_nodes(11) + block_nodes(12) + block_nodes(13, bold=True) + block_nodes(14) + add_examples("原理构造")
content.append(h(3, "温度计的种类")); content += block_nodes(15) + block_nodes(16) + block_nodes(17) + block_nodes(18) + block_nodes(19) + block_nodes(20)

# ═══ h2 温度计的使用 ═══
content.append(h(2, "温度计的使用"))
content.append(h(3, "温度计的使用方法"))
content += block_nodes(22, bold=True)
for i in range(23, 35): content += block_nodes(i)   # 含 B27 表 + 名师解读 plain + B34 图
content += add_examples("使用")
content.append(h(3, "温度计刻度换算"))
content.append(para("刻度均匀但不准确的温度计，其示数与真实温度成线性关系，可由两个已知对应点（如冰水0℃、沸水100℃）建立比例，换算任意示数对应的真实温度。"))
content += add_examples("换算")

# ═══ h2 体温计 ═══
content.append(h(2, "体温计"))
content.append(h(3, "体温计"))
content += block_nodes(41, bold=True) + block_nodes(42) + block_nodes(43) + block_nodes(44) + block_nodes(45) + block_nodes(46) + block_nodes(47) + block_nodes(48)
content += add_examples("体温计")

doc_json = {"type": "doc", "content": content}
title = "课时2 温度的测量"
h3s = [ "".join(x.get("text","") for x in b["content"]) for b in content if b["type"]=="heading" and b["attrs"]["level"]==3]
print(f"[组装] 总块={len(content)} | kgMindmap=1 | h3知识点={len(h3s)}: {h3s}")
kex = [b for b in content if b["type"]=="kgExample"]; print(f"        kgExample={len(kex)} | 图片={sum(1 for b in content if b['type']=='image')} | 表格={sum(1 for b in content if b['type']=='table')}")
open(os.path.join(BASE, "final_doc_lesson2.json"), "w", encoding="utf-8").write(json.dumps(doc_json, ensure_ascii=False, indent=1))

if APPLY:
    conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], database=cfg["database"], charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SELECT id FROM biz_kg_doc WHERE course_id=%s AND lesson_no=%s", (COURSE_ID, LESSON_NO))
    row = cur.fetchone(); dj = json.dumps(doc_json, ensure_ascii=False)
    if row:
        cur.execute("UPDATE biz_kg_doc SET doc_json=%s, title=%s WHERE id=%s", (dj, title, row[0])); print(f"[落库] 更新 doc id={row[0]}")
    else:
        cur.execute("INSERT INTO biz_kg_doc(course_id,book_id,lesson_no,title,doc_json,create_time) VALUES(%s,%s,%s,%s,%s,NOW())", (COURSE_ID, BOOK_ID, LESSON_NO, title, dj)); print("[落库] 新建")
    conn.commit(); conn.close()
else:
    print("(dry-run; 加 apply 落库)")
