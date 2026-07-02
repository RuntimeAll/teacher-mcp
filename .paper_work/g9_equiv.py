"""G9 等价回归：证明「拆题/转换逻辑上提 app/ 后与 PRD-C-208 改造前(git 基线)逐题字节等价」。

对多份真实 doc（同步练习 + 日常考试各若干）：
  ① 旧路径：old_omml.docx→text（基线 omml_to_text）→ old parse_paper（基线内联）
  ② 新路径：app.docconv.docx_to_text → app.paperparse.parse_paper
逐题 diff（题号/题型/stem/options/answer/analyze/score/has_fig/source）。全等 = 迁移无行为漂移。
不写库、不联网、不起服务——纯离线快照对照。
"""
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 新路径 ──
from app.docconv import docx_to_text as new_docx_to_text
from app.paperparse import parse_paper as new_parse

# ── 旧路径：动态载入基线两个文件（含内联 parse_paper / walk）──
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
try:
    from dwml.omml import oMath2Latex
except Exception:
    oMath2Latex = None


def old_docx_to_text(path):
    """复刻基线 omml_to_text.main 的转换（不含文件写）。"""
    def _ln(el):
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
        tag = _ln(el)
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
            ids = []
            for d in el.iter():
                for k, v in d.attrib.items():
                    if k.endswith("}embed") or k.endswith("}id"):
                        ids.append(v)
            parts.append("〖图:" + ",".join(ids) + "〗" if ids else "〖图〗")
            return
        for c in el:
            walk(c, parts)

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
    return "\n".join(lines)


# 旧 parse_paper：从基线文件动态载入（它 import app.dicts.section_type_of，仍在）
spec = importlib.util.spec_from_file_location("old_ip", str(ROOT / ".paper_work" / "old_ingest_paper.py"))
# 基线文件顶部有 mcp import，隔离掉：只抽 parse_paper 依赖的纯函数，用 exec 注入受控命名空间
_src = (ROOT / ".paper_work" / "old_ingest_paper.py").read_text(encoding="utf-8")
# 砍掉 mcp/asyncio 相关（parse 层不需要），保留到 parse_answer_section 之后的纯解析函数
_cut = _src.index("# ───────────────────────── 图片")
_head = _src[: _src.index("async def run")]
old_ns = {"__name__": "old_ip", "__file__": str(ROOT / ".paper_work" / "old_ingest_paper.py")}
# 去掉 from mcp / stdio 行
_clean = "\n".join(
    l for l in _head.splitlines()
    if not l.startswith(("from mcp", "from tools.dbutil", "import asyncio")) and "stdio_client" not in l
)
exec(compile(_clean, "old_head", "exec"), old_ns)
old_parse = old_ns["parse_paper"]

DOCS = [
    "预研空间/试卷下载/初中数学/同步练习/七上/1.3 绝对值#1.doc",
    "预研空间/试卷下载/初中数学/同步练习/七上/2.5 有理数的乘方#1.doc",
    "预研空间/试卷下载/初中数学/日常考试/七上/2024_2025学年浙江金华初一上学期期末数学试卷（开发区）.doc",
]
BASE = Path("D:/workplace/book-ai")

all_ok = True
for rel in DOCS:
    doc = BASE / rel
    old_text = old_docx_to_text(str(doc))
    new_text, _ = new_docx_to_text(str(doc))
    text_eq = old_text == new_text
    oq = old_parse(old_text)
    nq = new_parse(new_text)
    same = len(oq) == len(nq) and all(o == n for o, n in zip(oq, nq))
    print(f"{'✓' if (text_eq and same) else '✗'} {Path(rel).name}: 文本等={text_eq} | 旧{len(oq)}题 新{len(nq)}题 逐题等={same}")
    if not (text_eq and same):
        all_ok = False
        for i, (o, n) in enumerate(zip(oq, nq)):
            if o != n:
                print(f"    第{i+1}题差异:")
                for k in o:
                    if o[k] != n.get(k):
                        print(f"      {k}: OLD={o[k]!r} NEW={n.get(k)!r}")
                break
print()
print("G9", "PASS: 拆题/转换逻辑上提 app/ 后逐题字节等价，无行为漂移" if all_ok else "FAIL: 存在漂移(见上)")
sys.exit(0 if all_ok else 1)
