# -*- coding: utf-8 -*-
"""raw docx 节点索引 dump（供映射 subagent 用）：faithful_content 全文逐节点一行。
节点序号 = faithful_content 数组下标 = build_ir_from_mapping 的 ranges start/end 所指。
用法: python pipeline/dump_raw_nodes.py <docx路径> <out.txt>
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import lectureconv  # noqa: E402

DOCX = sys.argv[1]
OUT = sys.argv[2]

content, images, _ = lectureconv.faithful_content(DOCX)
lines = []
for i, n in enumerate(content):
    t = n.get("type")
    if t == "heading":
        txt = f"H{n.get('attrs', {}).get('level')} {lectureconv.node_text(n)}"
    elif t == "image":
        txt = f"imag 〖图:{n.get('attrs', {}).get('rid')}〗"
    elif t == "table":
        txt = "tabl [表] " + lectureconv.render_with_markers([n])[:100].replace("\n", " / ")
    else:
        txt = "para " + lectureconv.node_text(n)[:150]
    lines.append(f"[{i}] {txt}")
Path(OUT).write_text("\n".join(lines), encoding="utf-8")
print(f"raw 节点 dump {len(content)} 节点 → {OUT}")
