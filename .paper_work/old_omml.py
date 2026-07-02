"""docx → 结构化文本：段落内按文档序拼 w:t 正文 + $LaTeX$(OMML 经 dwml 转) + 〖图:rId〗。
纯 XML 结构解析，非 OCR。产物喂 ingest_paper.py 的 --txt（图靠 〖图:rId〗 标记，原 docx 走 --docx 抽图传 OSS）。

用法: .venv\\Scripts\\python.exe tools\\omml_to_text.py 原卷.doc 输出.txt
"""
import sys
import zipfile

from lxml import etree

try:
    from dwml.omml import oMath2Latex
except Exception:
    oMath2Latex = None

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def ln(el):
    return etree.QName(el).localname


def to_latex(om):
    if oMath2Latex is None:
        return "?"
    try:
        res = oMath2Latex(om)
        return getattr(res, "latex", None) or str(res)
    except Exception as e:
        return f"?({type(e).__name__})"


def walk(el, parts):
    tag = ln(el)
    ns = etree.QName(el).namespace
    if tag == "oMath" and ns == M:
        latex = to_latex(el).strip()
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
        walk(c, parts)


def main():
    path, out_path = sys.argv[1], sys.argv[2]
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = etree.fromstring(xml)
    body = root.find(f"{{{W}}}body")
    lines = []
    for p in body.findall(f"{{{W}}}p"):
        parts = []
        walk(p, parts)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"OK paras={len(lines)} chars={len(text)}")


if __name__ == "__main__":
    main()
