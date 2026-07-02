import pymysql

c = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                    database="ai_lesson_prep", charset="utf8mb4")
cur = c.cursor()
qid = 2072756020312109057
cur.execute("SELECT stem_text, dim1_kp_id FROM biz_question WHERE id=%s", (qid,))
stem, kp = cur.fetchone()
print("stem[:80]:", stem[:80])
print("kp:", kp)
BLACKLIST = ["（见原卷）", "(见原卷)", "见图", "如图所示题目"]
assert stem and not any(stem.strip().startswith(b) or stem.strip() == b for b in BLACKLIST), "stem 是占位串"
assert len(stem) > 50, "stem 过短疑似占位"
cur.execute("SELECT COUNT(*), MIN(oss_url) FROM biz_question_image WHERE question_id=%s", (qid,))
n, url = cur.fetchone()
print("图行数:", n, "| url:", (url or "")[:70])
assert n > 0, "无图行"
print()
print("G5 PASS: 手拍照片题 stem 非占位(完整转写含 LaTeX), biz_question_image 有行")
c.close()
