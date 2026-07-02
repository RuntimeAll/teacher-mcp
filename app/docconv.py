"""确定性文档转换（单一事实源）—— docx→结构化文本 / PDF→文字层检测+按页转图。

docx 路线从 tools/omml_to_text.py 上提（PRD-C-208 ⑤）：段落内按文档序拼 w:t 正文
+ $LaTeX$(OMML 经 dwml 转) + 〖图:rId〗。纯 XML 结构解析，非 OCR、零 LLM。
PDF 路线（PRD-C-208 3.3-a）：pymupdf 检文字层；🔴 H1a spike 判定公式抽取不可用
（上标丢/根指数错位/分数断裂）→ 数学/科学卷一律按页转图走多模态，文字层文本仅辅助。
"""
import zipfile

from lxml import etree

try:
    from dwml.omml import oMath2Latex
except Exception:
    oMath2Latex = None

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _ln(el):
    return etree.QName(el).localname


def _to_latex(om):
    if oMath2Latex is None:
        return "?"
    try:
        res = oMath2Latex(om)
        return getattr(res, "latex", None) or str(res)
    except Exception as e:
        return f"?({type(e).__name__})"


def _walk(el, parts):
    tag = _ln(el)
    ns = etree.QName(el).namespace
    if tag == "oMath" and ns == M:
        latex = _to_latex(el).strip()
        if latex:
            parts.append(f"${latex}$")
        return
    if tag == "t" and ns == W:
        if el.text:
            parts.append(el.text)
        return
    if tag in ("drawing", "pict"):
        # 抓 r:embed / r:id
        ids = []
        for d in el.iter():
            for k, v in d.attrib.items():
                if k.endswith("}embed") or k.endswith("}id"):
                    ids.append(v)
        parts.append("〖图:" + ",".join(ids) + "〗" if ids else "〖图〗")
        return
    for c in el:
        _walk(c, parts)


def docx_to_text(path):
    """docx（含 .doc 伪装）→ (结构化文本, 段落数)。真 OLE .doc（非 zip）→ 抛 BadZipFile，调用侧提示另存 docx。"""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = etree.fromstring(xml)
    body = root.find(f"{{{W}}}body")
    lines = []
    for p in body.findall(f"{{{W}}}p"):
        parts = []
        _walk(p, parts)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines), len(lines)


def extract_docx_images(docx_path, out_dir, prefix):
    """按 rId 抽 docx 全部媒体图到 out_dir，返回 {rId: 本地绝对路径}。与文本里的 〖图:rId〗 对应。"""
    from pathlib import Path

    from app.paperparse import load_docx_media

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    z, rid_map = load_docx_media(docx_path)
    result = {}
    import os
    for rid, tgt in rid_map.items():
        low = tgt.lower()
        if "media/" not in low.replace("\\", "/"):
            continue
        try:
            data = z.read("word/" + tgt.replace("\\", "/"))
        except KeyError:
            continue
        ext = os.path.splitext(tgt)[1] or ".png"
        fp = out / f"{prefix}_{rid}{ext}"
        fp.write_bytes(data)
        result[rid] = str(fp)
    z.close()
    return result


def pdf_probe_and_render(pdf_path, out_dir, prefix, dpi=170, max_pages=0):
    """pymupdf：检文字层 + 按页转图。返回 (has_text_layer, page_count, [页图路径], 文字层全文)。
    has_text_layer 仅供辅助（题号定位/纯文字题）——H1a 判定公式在文字层不可信，拆题以页图多模态为准。"""
    from pathlib import Path

    import fitz

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    n = doc.page_count
    texts, pages = [], []
    limit = min(n, max_pages) if max_pages else n
    for i in range(limit):
        pg = doc[i]
        texts.append(pg.get_text())
        pix = pg.get_pixmap(dpi=dpi)
        fp = out / f"{prefix}_p{i + 1}.png"
        pix.save(str(fp))
        pages.append(str(fp))
    text = "\n".join(texts)
    has_text = len(text.strip()) > 100 * max(limit, 1) * 0.5  # 平均每页>50字符≈有文字层
    doc.close()
    return has_text, n, pages, text
