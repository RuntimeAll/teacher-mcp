# -*- coding: utf-8 -*-
"""按「知识点↔节点区间映射」构建讲解 IR（映射式路线——docx 标题与 KG 知识点名对不上时的主路）。

映射 json 格式（由映射环节产出：批量=映射 subagent，试点=人工/主 agent）：
{
  "docx": "讲义docx绝对路径",
  "course_subject_id": "901001002003",
  "book_id": "CC7S",
  "ranges": [ {"kp_id": "901001002003001", "start": 5, "end": 13}, ... ]   // 节点区间闭区间，序=faithful_content 数组下标
}
产出：<batch>_ir.json（每 kp 一片段：H3 知识点名开头 + 原节点原样）+ <batch>_exercises_raw.txt（模块二/三习题原文）
用法: python pipeline/build_ir_from_mapping.py <mapping.json>
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
from app import db, lectureconv  # noqa: E402

WORK = ROOT / ".lecture_work"

mp = json.load(open(sys.argv[1], encoding="utf-8"))
DOCX = mp["docx"]
COURSE = mp["course_subject_id"]
BOOK = mp.get("book_id", "CC7S")
batch = Path(DOCX).stem.replace("#", "_").replace(" ", "_")

content, images_dict, stats = lectureconv.faithful_content(DOCX)
kg = db.kg_query("", parent_id=COURSE, limit=200)
id2name = {n["id"]: n["name"].strip() for n in kg}

# 覆盖闸：每个 KG 知识点必须有映射区间
mapped = {r["kp_id"] for r in mp["ranges"]}
missing = [id2name[k] for k in id2name if k not in mapped]
if missing:
    print(f"🔴 覆盖闸 FAIL：知识点无映射区间 {missing}")
    sys.exit(1)
bad = [r["kp_id"] for r in mp["ranges"] if r["kp_id"] not in id2name]
if bad:
    print(f"🔴 非法 kp_id（不在该课时 KG 下）: {bad}")
    sys.exit(1)

# 建片段：H3 知识点名 + 区间节点原样（同 kp 多区间按序拼接）
by_kp = {}
order = []
for r in sorted(mp["ranges"], key=lambda x: x["start"]):
    k = r["kp_id"]
    if k not in by_kp:
        by_kp[k] = []
        order.append(k)
    by_kp[k] += content[r["start"]:r["end"] + 1]

ir_frags = []
for k in order:
    nodes = [{"type": "heading", "attrs": {"level": 3},
              "content": [{"type": "text", "text": id2name[k]}]}] + by_kp[k]
    ir_frags.append({"subjectId": k, "kg_level": len(k) // 3, "title": id2name[k],
                     "contentJson": {"type": "doc", "content": nodes},
                     "stem_text": "".join(lectureconv.node_text(n) for n in nodes)})

WORK.mkdir(exist_ok=True)
ir_path = WORK / f"{batch}_ir.json"
ir_path.write_text(json.dumps({"book_id": BOOK, "course_subject_id": COURSE, "frags": ir_frags},
                              ensure_ascii=False, indent=1), encoding="utf-8")

# 习题原文：最后映射区间之后的首个「模块……」H3 起 → 文末
last_end = max(r["end"] for r in mp["ranges"])
ex_start = None
for i in range(last_end + 1, len(content)):
    n = content[i]
    if n.get("type") == "heading" and n.get("attrs", {}).get("level") == 3:
        t = "".join(lectureconv.node_text(n).split())
        if "习题" in t or "巩固" in t or t.startswith("模块"):
            ex_start = i
            break
ex_path = None
if ex_start is not None:
    ex_path = WORK / f"{batch}_exercises_raw.txt"
    ex_path.write_text(lectureconv.render_with_markers(content[ex_start:]), encoding="utf-8")

print(f"✅ IR: {ir_path}  片段={len(ir_frags)}（覆盖闸 PASS {len(mapped)}/{len(id2name)}）")
for f in ir_frags:
    print(f"   {f['subjectId']} «{f['title']}» 节点={len(f['contentJson']['content'])}")
print(f"习题原文: {ex_path or '（未检出习题模块）'}")
print(f"stats: {stats}")
