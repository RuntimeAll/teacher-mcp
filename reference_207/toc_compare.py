# -*- coding: utf-8 -*-
"""里外目录对比:讲义 doc 的 h3 标题(里面目录) vs KG 课时下 level5 知识点(外面目录)。
范本铁律(1.2.1 实证): doc 的 h3 标题必须与该课时 KG 知识点 1:1 对得上。
用法: python toc_compare.py <course_id> <lesson_no> <课时subject_id>
  例: python toc_compare.py 901001002 2 901001002002
"""
import io, sys, os, json, pymysql
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(BASE, "_dbcfg.json"), encoding="utf-8"))

COURSE_ID = sys.argv[1] if len(sys.argv) > 1 else "901001002"
LESSON_NO = int(sys.argv[2]) if len(sys.argv) > 2 else 2
KESHI_ID = sys.argv[3] if len(sys.argv) > 3 else "901001002002"

conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                       password=cfg["password"], database=cfg["database"], charset="utf8mb4")
cur = conn.cursor()

# 里面目录:doc 的 h3 标题(h2=专题组,不参与知识点比对)
cur.execute("SELECT doc_json FROM biz_kg_doc WHERE course_id=%s AND lesson_no=%s", (COURSE_ID, LESSON_NO))
row = cur.fetchone()
inner = []
if row:
    dj = json.loads(row[0])
    for b in dj.get("content", []):
        if b.get("type") == "heading" and b.get("attrs", {}).get("level") == 3:
            t = "".join(x.get("text", "") for x in b.get("content", [])).strip()
            if t:
                inner.append(t)

# 外面目录:KG 课时下 level5 知识点(按 id 序)
cur.execute("SELECT id,name FROM biz_subject WHERE id LIKE %s AND LENGTH(id)=15 ORDER BY id", (KESHI_ID + "%",))
outer = [r[1].strip() for r in cur.fetchall()]
conn.close()

def norm(s):  # 去空白做宽松比对
    return "".join(s.split())

inner_set = {norm(x): x for x in inner}
outer_set = {norm(x): x for x in outer}

print(f"=== 里外目录对比  course={COURSE_ID} lesson={LESSON_NO} 课时={KESHI_ID} ===\n")
print(f"里面目录(doc h3, {len(inner)}):")
for x in inner: print("   ", ("✅" if norm(x) in outer_set else "❌ 外面无此项"), x)
print(f"\n外面目录(KG 知识点, {len(outer)}):")
for x in outer: print("   ", ("✅" if norm(x) in inner_set else "⚠️ 里面缺"), x)

matched = [outer_set[k] for k in outer_set if k in inner_set]
only_in = [inner_set[k] for k in inner_set if k not in outer_set]
missing = [outer_set[k] for k in outer_set if k not in inner_set]
print(f"\n── 判定 ──")
print(f"  对齐: {len(matched)}/{len(outer)}")
print(f"  里面多出(外面无): {only_in or '无'}")
print(f"  外面缺失(里面无): {missing or '无'}")
verdict = "PASS ✅ 里外目录完全对齐" if not only_in and not missing else "FAIL ❌ 里外目录未对齐"
print(f"  裁定: {verdict}")
sys.exit(0 if not only_in and not missing else 1)
