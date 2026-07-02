import pymysql

c = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                    database="ai_lesson_prep", charset="utf8mb4")
cur = c.cursor()
g4a = [2072754956556922882, 2072754956749860866]
g4b = [2072755482652667905, 2072755483122429953]
ids = g4a + g4b
fmt = ",".join(["%s"] * len(ids))
cur.execute(f"SELECT id, dim1_kp_id, stem_text FROM biz_question WHERE id IN ({fmt})", ids)
rows = cur.fetchall()
for qid, kp, stem in rows:
    print(qid, kp, "| $..$:", "$" in stem, "|", stem[:50])
assert len(rows) == 4, f"应 4 题, 实际 {len(rows)}"
assert all("$" in r[2] for r in rows), "有题干无 $..$ 公式"
fmt_b = ",".join(["%s"] * len(g4b))
cur.execute(f"SELECT question_id, COUNT(*) FROM biz_question_image WHERE question_id IN ({fmt_b}) GROUP BY question_id", g4b)
im = cur.fetchall()
print("扫描题图行:", im)
assert len(im) == 2 and all(n > 0 for _, n in im), "扫描题应有图"
print()
print("G4 PASS: 文字层(降级转图)2题 + 扫描2题 = 人工核对题数, 公式含$..$非空壳, 扫描题图入 biz_question_image")
c.close()
