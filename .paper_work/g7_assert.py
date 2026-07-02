import json

import pymysql

c = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                    database="ai_lesson_prep", charset="utf8mb4")
cur = c.cursor()
qid = 2072756784342331394
cur.execute("SELECT dim1_kp_id, difficult FROM biz_question WHERE id=%s", (qid,))
kp, diff = cur.fetchone()
print("dim1_kp_id:", kp, "| difficult:", diff)
assert str(kp).startswith("90"), f"科学题 dim1_kp 应 LIKE 90%, 实际 {kp}"
assert diff and diff > 0, "difficult 未打"
cur.execute("SELECT tags FROM biz_question_ai WHERE question_id=%s", (qid,))
tags = cur.fetchone()[0]
parsed = json.loads(tags) if tags else []
print("tags:", parsed)
assert isinstance(parsed, list) and len(parsed) > 0, "free_tags 未落 biz_question_ai.tags 或非数组"
cur.execute("SELECT knowledge_id FROM biz_question_knowledge WHERE question_id=%s AND is_primary=1", (qid,))
km = cur.fetchone()
print("主考点:", km)
assert km and str(km[0]).startswith("90"), "科学 KG 主考点关系未锚"
print()
print("G7 PASS: 科学题 dim1_kp LIKE 90.. + difficult 已打 + free_tags 非空数组(对齐预研轻打标)")
c.close()
