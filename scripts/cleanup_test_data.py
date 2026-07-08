# -*- coding: utf-8 -*-
"""PRD-O-005 测试数据清理（可复用）：按 [PRD-O-005-TEST] 标记 + G4 变式簇精确窗口删除。

范围（每轮 gate 跑完可重复执行）：
  题：标记题 + G4 变式簇（举一反三/admin/精确秒窗，断言恰好 3 行否则中止）
     级联：knowledge/ai/image/model/free_tag/paper_question + biz_text_content(三个 content_id)
  卷：名称带标记 → biz_paper_question + biz_paper
  备课：biz_student(名带标记) → biz_course_plan(名带标记或挂测试学生) → lesson → session → prep_pack
🔴 只删测试标记数据；雪花 id 全程 CAST(id AS CHAR) 防截尾；先 SELECT 打印再 DELETE。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\workplace\ai-bkb\codeplace-O\teacher-mcp\src")

from teacher_mcp.backends.db import conn  # noqa: E402

MARK = "%PRD-O-005-TEST%"
DRY = "--dry" in sys.argv


def rows(cur, sql, args=()):
    cur.execute(sql, args)
    return [r[0] for r in cur.fetchall()]


def delete(cur, table, col, ids, label=""):
    if not ids:
        print(f"  {table:28s} 0 行")
        return 0
    fmt = ",".join(["%s"] * len(ids))
    if DRY:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({fmt})", ids)
        n = cur.fetchone()[0]
    else:
        cur.execute(f"DELETE FROM {table} WHERE {col} IN ({fmt})", ids)
        n = cur.rowcount
    print(f"  {table:28s} {n} 行 {label}")
    return n


c = conn()
cur = c.cursor()

# ── 1. 题 id 收集（CAST CHAR 防雪花截尾）──
qids = rows(cur, "SELECT CAST(id AS CHAR) FROM biz_question WHERE stem_text LIKE %s", (MARK,))
print(f"标记题: {len(qids)} 道")

variant_ids = []
for win in (("2026-07-07 11:43:49", "2026-07-07 11:43:51"),
            ("2026-07-07 19:43:49", "2026-07-07 19:43:51")):
    variant_ids = rows(cur,
        "SELECT CAST(id AS CHAR) FROM biz_question WHERE import_source='举一反三'"
        " AND create_user=1 AND create_time BETWEEN %s AND %s", win)
    if variant_ids:
        break
if len(variant_ids) == 0:
    print("G4 变式簇: 0 道（已清理过，跳过）")
elif len(variant_ids) != 3:
    print(f"🔴 中止：G4 变式簇应为 0（已清）或 3 行，实得 {len(variant_ids)}: {variant_ids}（人工核）")
    sys.exit(1)
else:
    print(f"G4 变式簇: 3 道 {variant_ids}")
qids += variant_ids

# 文本内容 id（删 biz_question 前先取）
tc_ids = []
if qids:
    fmt = ",".join(["%s"] * len(qids))
    cur.execute(
        f"SELECT stem_text_content_id, answer_text_content_id, analyze_text_content_id"
        f" FROM biz_question WHERE id IN ({fmt})", qids)
    for r in cur.fetchall():
        tc_ids += [str(x) for x in r if x]

# ── 2. 卷 ──
pids = rows(cur, "SELECT CAST(id AS CHAR) FROM biz_paper WHERE name LIKE %s", (MARK,))
print(f"标记卷: {len(pids)} 张")

# ── 3. 备课对象 ──
sids = rows(cur, "SELECT CAST(id AS CHAR) FROM biz_student WHERE name LIKE %s", (MARK,))
plan_ids = rows(cur,
    "SELECT CAST(id AS CHAR) FROM biz_course_plan WHERE name LIKE %s"
    " OR (target_type='0' AND CAST(target_id AS CHAR) IN"
    f" ({','.join(['%s']*len(sids)) if sids else 'NULL'}))",
    (MARK, *sids) if sids else (MARK,))
lesson_ids = []
if plan_ids:
    fmt = ",".join(["%s"] * len(plan_ids))
    lesson_ids = rows(cur, f"SELECT CAST(id AS CHAR) FROM biz_course_plan_lesson WHERE plan_id IN ({fmt})", plan_ids)
print(f"测试学生: {len(sids)} | 计划: {len(plan_ids)} | 课次: {len(lesson_ids)}")

print(f"\n{'[DRY RUN] ' if DRY else ''}开始删除：")
# 题级联（子表先删）
for t in ("biz_question_knowledge", "biz_question_ai", "biz_question_image",
          "biz_question_model", "biz_question_free_tag", "biz_paper_question"):
    delete(cur, t, "question_id", qids)
delete(cur, "biz_text_content", "id", tc_ids)
delete(cur, "biz_question", "id", qids)
# 卷
delete(cur, "biz_paper_question", "paper_id", pids)
delete(cur, "biz_paper", "id", pids)
# 备课链（叶到根）
delete(cur, "biz_prep_pack", "plan_lesson_id", lesson_ids)
delete(cur, "biz_schedule_session", "plan_lesson_id", lesson_ids)
delete(cur, "biz_schedule_session", "target_id", sids, "(挂学生的散场次)")
delete(cur, "biz_course_plan_lesson", "plan_id", plan_ids)
delete(cur, "biz_course_plan", "id", plan_ids)
delete(cur, "biz_student", "id", sids)

if not DRY:
    c.commit()

# ── 验证 ──
n1 = rows(cur, "SELECT COUNT(*) FROM biz_question WHERE stem_text LIKE %s", (MARK,))[0]
n2 = rows(cur, "SELECT COUNT(*) FROM biz_paper WHERE name LIKE %s", (MARK,))[0]
n3 = rows(cur, "SELECT COUNT(*) FROM biz_student WHERE name LIKE %s", (MARK,))[0]
n4 = rows(cur, "SELECT COUNT(*) FROM biz_course_plan WHERE name LIKE %s", (MARK,))[0]
print(f"\n残留验证: 题={n1} 卷={n2} 学生={n3} 计划={n4}（应全 0）")
c.close()
sys.exit(0 if (n1 == n2 == n3 == n4 == 0 or DRY) else 2)
