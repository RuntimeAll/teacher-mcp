# -*- coding: utf-8 -*-
"""机械校验闸：subagent 产物（parsed_items.json / examples.json）→ 入库前的确定性校验。
批量无人值守跑，靠这个闸拦解析错误，不靠人眼。
用法: python pipeline/validate_parsed.py <parsed.json> <course_subject_id> [exercises_raw.txt]
校验项:
  1. kp_id 全部真实存在于该课时 KG 下
  2. 必填字段齐（stem/answer/kp_id 或 kp_subjectId）
  3. stem 里〖图:rId〗与 image_rids 一致
  4. 题数 == 原文【答案】标记数（给了 raw 才查；examples 无 raw 跳过）
  5. stem 无【答案】/【详解】残留（拆分干净）
exit 0=PASS / 1=FAIL（打印明细）
"""
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402

parsed = json.load(open(sys.argv[1], encoding="utf-8"))
course = sys.argv[2]
raw_path = sys.argv[3] if len(sys.argv) > 3 else None

items = parsed.get("items") or parsed.get("examples") or []
errs = []

valid_kp = {n["id"] for n in db.kg_query("", parent_id=course, limit=200)}
for i, it in enumerate(items, 1):
    kp = it.get("kp_id") or it.get("kp_subjectId")
    if kp not in valid_kp:
        errs.append(f"题{i}: kp_id 非法 {kp}")
    if not (it.get("stem") or "").strip():
        errs.append(f"题{i}: 缺 stem")
    if not str(it.get("answer", "")).strip():
        errs.append(f"题{i}: 缺 answer")
    stem = it.get("stem", "")
    in_stem = set(re.findall(r"〖图:(rId\d+)〗", stem))
    declared = set(it.get("image_rids", []))
    if in_stem != declared:
        errs.append(f"题{i}: 图标记不一致 stem={sorted(in_stem)} declared={sorted(declared)}")
    if "【答案】" in stem or "【详解】" in stem:
        errs.append(f"题{i}: stem 混入答案/详解")

if raw_path:
    raw = Path(raw_path).read_text(encoding="utf-8")
    n_ans = len(re.findall(r"【答案】|答案[：:]", raw))
    if len(items) != n_ans:
        errs.append(f"题数({len(items)}) ≠ 原文答案标记数({n_ans})——漏题或过拆")

if errs:
    print(f"🔴 校验 FAIL（{len(errs)} 项）:")
    for e in errs:
        print("  ", e)
    sys.exit(1)
print(f"✅ 校验 PASS：{len(items)} 题，kp 全合法，图一致，stem 无答案残留" +
      (f"，题数与答案标记数吻合" if raw_path else ""))
