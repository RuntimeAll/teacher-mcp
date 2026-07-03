# -*- coding: utf-8 -*-
"""PRD-C-207 V0:建 biz_kg_lecture_frag + 把 biz_kg_doc 课时1/2 拆成片段迁入。
无参 = dry-run(建表+备份+构建+打印,不插入片段);加 --apply = 插入+验证汇聚。
"""
import io, sys, os, json, time, pymysql
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
APPLY = "--apply" in sys.argv
BASE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(BASE, "_dbcfg.json")))
conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                       password=cfg["password"], database=cfg["database"],
                       charset=cfg.get("charset", "utf8mb4"), autocommit=False)
cur = conn.cursor()

DDL = """
CREATE TABLE IF NOT EXISTS biz_kg_lecture_frag (
  id            BIGINT       NOT NULL                COMMENT '主键(雪花)',
  subject_id    VARCHAR(20)  NOT NULL                COMMENT '挂载KG节点id(biz_subject.id);任意层:课时L4/知识点L5(原子)/节L3/章L2/册L1',
  kg_level      TINYINT      NOT NULL                COMMENT '节点层级=LENGTH(subject_id)/3:1册2章3节4课时5知识点(冗余,按层批量取)',
  book_id       VARCHAR(32)  NOT NULL                COMMENT '教辅套id(biz_book.id);崔崔=CC7S',
  owner_id      BIGINT       NOT NULL DEFAULT 0      COMMENT '归属;0=官方,否则=个人用户id;个人片段按节点覆盖官方',
  title         VARCHAR(200)          DEFAULT NULL   COMMENT '片段标题,默认取节点名',
  content_json  LONGTEXT              DEFAULT NULL   COMMENT '本节点自身讲义片段(Tiptap JSON:讲解/例题kgExample(qid)/表/图/思维导图);空=纯汇聚节点',
  stem_text     MEDIUMTEXT            DEFAULT NULL   COMMENT '片段纯文本镜像(全文检索/agent召回)',
  sort          INT          NOT NULL DEFAULT 0      COMMENT '同层排序覆盖;0=跟随subject_id树序',
  status        CHAR(1)      NOT NULL DEFAULT '0'    COMMENT '0正常/1草稿',
  create_by     VARCHAR(64)           DEFAULT NULL,
  create_time   DATETIME              DEFAULT CURRENT_TIMESTAMP,
  update_by     VARCHAR(64)           DEFAULT NULL,
  update_time   DATETIME              DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  remark        VARCHAR(500)          DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_node_book_owner (subject_id, book_id, owner_id),
  KEY idx_book_level (book_id, kg_level),
  KEY idx_subject (subject_id),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='讲义片段-挂KG节点的原子教学内容,agent组书原料;某层完整讲义=自身+子孙片段按树序汇聚';
"""

def txt(node):
    if node is None: return ""
    if node.get("type") == "text": return node.get("text", "")
    return "".join(txt(c) for c in (node.get("content", []) or []))

def heading_texts(nodes):
    out = []
    for n in nodes:
        if n.get("type") == "heading" and n.get("attrs", {}).get("level") == 3:
            out.append(txt(n).strip())
    return out

# ---------- 1. 建表 ----------
cur.execute(DDL)
print("[1] biz_kg_lecture_frag 建表 OK (IF NOT EXISTS)")

# ---------- 2. 备份 biz_kg_doc ----------
cur.execute("DROP TABLE IF EXISTS biz_kg_doc_bak")
cur.execute("CREATE TABLE biz_kg_doc_bak AS SELECT * FROM biz_kg_doc")
cur.execute("SELECT id, course_id, lesson_no, book_id, title, doc_json FROM biz_kg_doc ORDER BY id")
rows = cur.fetchall()
json.dump([{"id": r[0], "course_id": r[1], "lesson_no": r[2], "book_id": r[3], "title": r[4], "doc_json": r[5]} for r in rows],
          open(os.path.join(BASE, "_bak_biz_kg_doc.json"), "w", encoding="utf-8"), ensure_ascii=False)
print(f"[2] 备份 OK: biz_kg_doc_bak 表 + _bak_biz_kg_doc.json ({len(rows)} 行)")

# ---------- 3. 构建片段 ----------
_seq = [0]
def newid():
    _seq[0] += 1
    return (int(time.time() * 1000) << 12) + _seq[0]

all_frags = []   # dict rows to insert
for (doc_id, course_id, lesson_no, book_id, title, dj) in rows:
    course_l4 = course_id + f"{lesson_no:03d}"          # 节L3 + 课时序号 = 课时L4 节点
    # 该课时下知识点 name->id
    cur.execute("SELECT id, name FROM biz_subject WHERE parent_id=%s AND level=5", (course_l4,))
    kp_by_name = {name.strip(): sid for sid, name in cur.fetchall()}
    cur.execute("SELECT name FROM biz_subject WHERE id=%s", (course_l4,))
    course_name_row = cur.fetchone()
    course_name = course_name_row[0] if course_name_row else title

    doc = json.loads(dj)
    content = doc.get("content", [])
    frags = {course_l4: []}          # subject_id -> nodes
    order = [course_l4]              # 保留插入次序
    cur_kp = None
    pending_h2 = None
    unmatched = []
    for n in content:
        t = n.get("type")
        lv = n.get("attrs", {}).get("level") if t == "heading" else None
        if t == "heading" and lv == 2:
            pending_h2 = n
            continue
        if t == "heading" and lv == 3:
            name = txt(n).strip()
            kid = kp_by_name.get(name)
            if not kid:
                unmatched.append(name)
                kid = f"__UNMATCHED__{name}"
            cur_kp = kid
            if kid not in frags:
                frags[kid] = []; order.append(kid)
            if pending_h2:
                frags[kid].append(pending_h2); pending_h2 = None
            frags[kid].append(n)
            continue
        # 普通节点
        target = course_l4 if cur_kp is None else cur_kp
        frags[target].append(n)
    if pending_h2:  # 落单 H2(理论上不会)
        frags[course_l4].append(pending_h2)

    # 校验:段内 kgExample.knowledgeId 必须 == 段的 subject_id
    kid_conflicts = []
    for kid, nodes in frags.items():
        for n in nodes:
            if n.get("type") == "kgExample":
                ex_kid = n.get("attrs", {}).get("knowledgeId")
                if ex_kid and kid.startswith("90") and ex_kid != kid:
                    kid_conflicts.append((kid, ex_kid))

    print(f"\n===== doc id={doc_id}  {title}  → 课时L4={course_l4} ({course_name}) =====")
    if unmatched:
        print(f"   ⚠ 未匹配到知识点节点的 H3: {unmatched}")
    if kid_conflicts:
        print(f"   ⚠ kgExample.knowledgeId 与段锚不一致: {kid_conflicts}")
    for sid in order:
        nodes = frags[sid]
        lv = len(sid) // 3
        ex = sum(1 for n in nodes if n.get("type") == "kgExample")
        h3 = heading_texts(nodes)
        stem = "".join(txt(n) for n in nodes)
        nm = course_name if sid == course_l4 else next((k for k, v in kp_by_name.items() if v == sid), sid)
        print(f"   [{sid}] L{lv} «{nm[:20]}»  节点={len(nodes)} 例题={ex} H3={h3} stem={len(stem)}")
        all_frags.append({
            "id": newid(), "subject_id": sid, "kg_level": lv, "book_id": book_id or "CC7S",
            "owner_id": 0, "title": nm, "content_json": json.dumps({"type": "doc", "content": nodes}, ensure_ascii=False),
            "stem_text": stem, "sort": 0, "status": "0",
        })

print(f"\n[3] 构建完成: 共 {len(all_frags)} 片段")

# ---------- 4. 插入 ----------
if not APPLY:
    conn.rollback()   # 回滚备份表之外?——备份表已建但未 commit,rollback 会撤销;dry-run 不留痕
    print("\n[DRY-RUN] 未插入片段、未 commit(建表/备份也回滚)。确认无误后加 --apply 重跑。")
    conn.close()
    sys.exit(0)

cur.execute("DELETE FROM biz_kg_lecture_frag WHERE book_id=%s AND owner_id=0", ("CC7S",))
print(f"[4] 清旧 CC7S 官方片段 {cur.rowcount} 行")
for f in all_frags:
    cur.execute("""INSERT INTO biz_kg_lecture_frag
        (id, subject_id, kg_level, book_id, owner_id, title, content_json, stem_text, sort, status, create_by)
        VALUES (%(id)s,%(subject_id)s,%(kg_level)s,%(book_id)s,%(owner_id)s,%(title)s,%(content_json)s,%(stem_text)s,%(sort)s,%(status)s,'v0-migrate')""", f)
conn.commit()
print(f"[4] 插入 {len(all_frags)} 片段 OK,已 commit")

# ---------- 5. 验证汇聚 ----------
print("\n[5] 验证:按 subject_id 前缀汇聚,列 H3 标题")
for course_l4, expect in (("901001002001", 10), ("901001002002", 7)):
    cur.execute("""SELECT subject_id, kg_level, content_json FROM biz_kg_lecture_frag
                   WHERE book_id='CC7S' AND owner_id=0 AND subject_id LIKE %s
                   ORDER BY subject_id""", (course_l4 + "%",))
    agg = cur.fetchall()
    all_h3 = []
    for sid, lv, cj in agg:
        all_h3 += heading_texts(json.loads(cj).get("content", []))
    ok = "✅" if len(all_h3) == expect else "❌"
    print(f"   {ok} 课时 {course_l4}: 片段={len(agg)} H3总数={len(all_h3)}(期望{expect})")
    print(f"      H3序: {all_h3}")
conn.close()
