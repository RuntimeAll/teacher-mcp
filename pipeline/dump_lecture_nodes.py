# -*- coding: utf-8 -*-
"""从讲解 IR 生成节点索引 dump（供例题提取 subagent 定位例题节点段）。
用法: python pipeline/dump_lecture_nodes.py <讲解IR.json> <out.txt>
IR 格式 = convert_lecture_docx(mode='cuicui') 产出的 {frags:[{subjectId,title,contentJson}]}
"""
import json
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

IR = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else IR.replace(".json", "_nodes.txt")

ir = json.load(open(IR, encoding="utf-8"))
blocks = []
for f in ir["frags"]:
    nodes = f["contentJson"]["content"]
    lines = [f"### 知识点片段 subjectId={f['subjectId']} title={f['title']} (共{len(nodes)}节点)"]
    for i, n in enumerate(nodes):
        t = n.get("type")
        if t == "image":
            txt = f"〖图:{n.get('attrs', {}).get('rid')}〗"
        elif t == "heading":
            txt = f"[H{n.get('attrs', {}).get('level')}] {lectureconv.node_text(n)}"
        elif t == "table":
            txt = "[表] " + lectureconv.render_with_markers([n])[:80]
        else:
            txt = lectureconv.node_text(n)
        lines.append(f"  n{i}: {txt[:120]}")
    blocks.append("\n".join(lines))
Path(OUT).write_text("\n\n".join(blocks), encoding="utf-8")
print(f"节点 dump → {OUT}")
