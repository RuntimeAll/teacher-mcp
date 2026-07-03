# -*- coding: utf-8 -*-
"""习题 bulk 入库 runner：解析 subagent 产物(parsed_items) → 补图 local_path → ingest_items 一次入库。
用法: python .lecture_work/ingest_exercises.py <parsed_items.json> <docx路径> <subject_root>
"""
import json
import os
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

PARSED = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / ".lecture_work" / "1.2.1_parsed_items.json")
DOCX = sys.argv[2] if len(sys.argv) > 2 else r"D:\workplace\book-ai\预研空间\2025秋七上科学崔崔老师讲义\1.2.1科学测量——长度的测量  体积的测量（教师版）.docx"
SUBJECT_ROOT = sys.argv[3] if len(sys.argv) > 3 else "901"

# 图 rid → 本地路径
_, images_dict, _ = lectureconv.faithful_content(DOCX)
imgs = lectureconv.extract_images(DOCX, str(ROOT / ".lecture_imgs"), "1.2.1_teacher", images_dict)
rid2path = {i["rid"]: i["local_path"] for i in imgs}

parsed = json.load(open(PARSED, encoding="utf-8"))
items = parsed["items"]
batch = {"subject_root": SUBJECT_ROOT, "items": []}
missing_img = []
for it in items:
    ing = {
        "stem": it["stem"],
        "options": it.get("options", []),
        "answer": it.get("answer", ""),
        "analyze": it.get("analyze", ""),
        "question_type": it.get("question_type"),
        "kp_id": it["kp_id"],
    }
    if it.get("source_raw"):
        ing["source_raw"] = it["source_raw"]
    im = []
    for rid in it.get("image_rids", []):
        p = rid2path.get(rid)
        if p:
            im.append({"local_path": p, "rid": rid, "role": "stem"})
        else:
            missing_img.append(rid)
    if im:
        ing["images"] = im
    batch["items"].append(ing)

out = ROOT / ".lecture_work" / "1.2.1_ingest_batch.json"
out.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"batch 就绪 {len(batch['items'])} 题 → {out}")
if missing_img:
    print("⚠ 缺本地图 rid:", missing_img)
# kp 分布
from collections import Counter
print("kp 分布:", dict(Counter(i["kp_id"] for i in batch["items"])))
