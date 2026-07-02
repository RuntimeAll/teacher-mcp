"""docx → 结构化文本 CLI（薄壳）：逻辑上提 app/docconv.py（PRD-C-208 ⑤，与 MCP 工具 convert_doc 同源）。

段落内按文档序拼 w:t 正文 + $LaTeX$(OMML 经 dwml 转) + 〖图:rId〗。纯 XML 结构解析，非 OCR。
产物喂 ingest_paper.py 的 --txt（图靠 〖图:rId〗 标记，原 docx 走 --docx 抽图传 OSS）。

用法: .venv\\Scripts\\python.exe tools\\omml_to_text.py 原卷.doc 输出.txt
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.docconv import docx_to_text  # noqa: E402


def main():
    path, out_path = sys.argv[1], sys.argv[2]
    text, paras = docx_to_text(path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"OK paras={paras} chars={len(text)}")


if __name__ == "__main__":
    main()
