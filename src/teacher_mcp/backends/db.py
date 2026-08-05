"""app 层 pymysql 收口（PRD-C-208 3.3-b 拍板：本期关系表在 MCP 工具内封 pymysql）。

🔴 范围收口（AC8 走查面）：只做现有 HTTP 端点覆盖不了的四件事——
  ① biz_question_model 模型链 + biz_solution_model 新模型提议（propose 串行，防并发撞 TY 主键）
  ② biz_question_ai.difficulty_reason（why 难度依据；IngestAiBo 无此字段位）
  ③ biz_paper.subject_id 卷目录补设（page() 按它筛目录，/create 不设）
  ④ biz_subject 只读查表（resolve_kg 锚定）
  ⑤ 溯源找回只读检索（recent_questions/papers/lecture_frags；HTTP page 无 batch/create_user 维度）
其余（题/图/知识点关系/DNA blob/难度）一律走 RuoYi HTTP。
🔴 技术债显式挂账：对外开放（stdio→HTTP）前须 HTTP 化（A 线补端点，独立卡）。
🔴 凭据从 .env 读（config.Settings），dev 默认 root/123456@127.0.0.1:3307/ai_lesson_prep。
"""
import pymysql

from teacher_mcp.config import settings


def conn():
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_database, charset="utf8mb4",
    )


# ───────────────────────── ① 模型链 + 新模型提议 ─────────────────────────

def propose_models(records):
    """new_models[]（现有模型覆盖不了的招式）建档入 biz_solution_model
    （status='2'=待转正，model_kind='derived'，id 续 TY 序号；按 name 去重，已提议过则复用），
    并把生成的 model_id 回填进该题 records[*]['models']（is_primary 继承 new_models 项）。返回 (新建数, name→id)。
    🔴 全程单连接串行（七上教训：并发 propose 撞 TY 主键）。"""
    c = conn()
    created = 0
    name2id = {}
    with c.cursor() as cur:
        cur.execute("SELECT CAST(id AS CHAR),name FROM biz_solution_model")
        existing = {name: cid for cid, name in cur.fetchall()}
        cur.execute("SELECT CAST(id AS CHAR) FROM biz_solution_model WHERE id LIKE 'TY%'")
        maxty = max([int(r[0][2:]) for r in cur.fetchall() if r[0][2:].isdigit()] or [0])
        for r in records:
            for nm in r.get("new_models", []) or []:
                name = nm["name"]
                mid = existing.get(name)
                if not mid:
                    maxty += 1
                    mid = f"TY{maxty:02d}"
                    cur.execute(
                        "INSERT INTO biz_solution_model(id,name,model_kind,category,trigger_feature,"
                        "action_conclusion,is_gold,difficulty_tier,freq_band,sort,status,create_time)"
                        " VALUES(%s,%s,'derived',%s,%s,%s,0,%s,%s,0,'2',NOW())",
                        (mid, name, nm.get("category", "通用"), nm.get("trigger_feature", ""),
                         nm.get("action_conclusion", ""), nm.get("difficulty_tier", 2), nm.get("freq_band", 1)))
                    existing[name] = mid
                    created += 1
                name2id[name] = mid
                r.setdefault("models", []).append({"model_id": mid, "is_primary": int(nm.get("is_primary", 0))})
        c.commit()
    c.close()
    return created, name2id


def write_models(records):
    """逐题写 biz_question_model（models=[{model_id,is_primary}]）。增量幂等（按 q+model 去重）。返回新增数。"""
    c = conn()
    n = 0
    with c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id),0) FROM biz_question_model")
        base = cur.fetchone()[0]
        for r in records:
            qid = int(r["question_id"])
            for m in r.get("models", []) or []:
                mid, prim = m["model_id"], int(m.get("is_primary", 0))
                cur.execute("SELECT 1 FROM biz_question_model WHERE question_id=%s AND model_id=%s LIMIT 1", (qid, mid))
                if cur.fetchone():
                    continue
                base += 1
                cur.execute("INSERT INTO biz_question_model(id,question_id,model_id,is_primary,source,role)"
                            " VALUES(%s,%s,%s,%s,'AI',%s)", (base, qid, mid, prim, "主" if prim else "辅"))
                n += 1
        c.commit()
    c.close()
    return n


# ───────────────────────── ② 难度依据 ─────────────────────────

def set_difficulty_reason(question_id, why):
    """biz_question_ai.difficulty_reason = why（该行已由 /teacher/ingest/ai upsert，存在为前提；无行则跳过返回 False）。"""
    c = conn()
    ok = False
    with c.cursor() as cur:
        cur.execute("UPDATE biz_question_ai SET difficulty_reason=%s WHERE question_id=%s",
                    ((why or "")[:500], int(question_id)))
        ok = cur.rowcount > 0
        c.commit()
    c.close()
    return ok


# ───────────────────────── ②b 前缀清洗残留检查（只读，灌库铁律验证端）─────────────────────────

# 与 paperparse.SOURCE_PREFIX 同门控词（MySQL REGEXP/ICU 方言版；ASCII 方括号类在 ICU 转义诡异且中文卷罕见，
# 检查端只盯 （(【 三种开头——Python 预防端 strip_source_prefix 仍全支持）
_RESIDUE_REGEXP = ("^[（(【][^）)】]{1,40}(真题|中考|高考|会考|竞赛|模拟|期末|期中|月考|学年|"
                   "单元测试|质检|调研|联考|检测|专题练习|假期作业|20[0-9]{2})")


def find_prefix_residues(paper_id=None, question_ids=None, limit=50):
    """查题干开头来源前缀残留（灌库后必验 REGEXP=0 铁律）。只读。返回 [{id, head}]。"""
    c = conn()
    with c.cursor() as cur:
        if paper_id:
            cur.execute(
                "SELECT q.id, LEFT(q.stem_text, 50) FROM biz_question q"
                " JOIN biz_paper_question pq ON pq.question_id=q.id AND pq.paper_id=%s"
                " WHERE q.stem_text REGEXP %s LIMIT %s", (int(paper_id), _RESIDUE_REGEXP, int(limit)))
        elif question_ids:
            fmt = ",".join(["%s"] * len(question_ids))
            cur.execute(
                f"SELECT id, LEFT(stem_text, 50) FROM biz_question WHERE id IN ({fmt})"
                " AND stem_text REGEXP %s LIMIT %s",
                (*[int(q) for q in question_ids], _RESIDUE_REGEXP, int(limit)))
        else:
            return []
        out = [{"id": r[0], "head": r[1]} for r in cur.fetchall()]
    c.close()
    return out


# ───────────────────────── ③ 卷目录补设 ─────────────────────────

def set_paper_subject(paper_id, category_id):
    """建卷后补设 biz_paper.subject_id=目录节点 id（page 按它 likeRight 筛目录，卷才在目录下可见）。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("UPDATE biz_paper SET subject_id=%s WHERE id=%s", (str(category_id), int(paper_id)))
        c.commit()
    c.close()


# ───────────────────────── ④b 题图 oss_url 只读查表（举一反三入口用）─────────────────────────

def question_image_url(question_id):
    """取某题的图 oss_url（biz_question_image，举一反三入口只认图片 URL）。
    优先 role='stem' 的图，其次任意 http 图（按 seq）。无图 → None。
    🔴 现实数据：biz_question.stem_img_url 常 NULL，图只在 biz_question_image——故此处直查该表
       （HTTP /teacher/question/list 的 stemImg 覆盖不全）。只读，挂账同 db.py 头注释。"""
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT oss_url, role, seq FROM biz_question_image"
                " WHERE question_id=%s AND oss_url LIKE 'http%%'"
                " ORDER BY (role='stem') DESC, seq ASC, id ASC LIMIT 1",
                (int(question_id),))
            row = cur.fetchone()
    finally:
        c.close()
    return row[0] if row else None


# ───────────────────────── ⑤ 溯源找回（只读，PRD-O-005 溯源增强）─────────────────────────
# 双管道语义：import_source 以 'mcp-' 前缀 = MCP 机录（mcp-ingest/mcp-data/mcp-all…）；
# 不带 mcp- 前缀（main/手工导入/'举一反三'引擎…）= 其他管道。按 import_batch_id / create_time /
# create_user 只读检索，不依赖 stem LIKE（变式题 stem_text 常 NULL，关键词搜漏它们）。

def recent_questions(uid=None, batch_id="", since_dt=None, mine=False,
                     subject_id="", question_type=None, difficult=None,
                     limit=100, offset=0):
    """按 import_batch_id / create_time / create_user 只读检索题（快速找回刚录的题；不走 stem LIKE）。
    过滤项任意组合：batch_id（精确批次）/ since_dt（create_time>=）/ mine（create_user=uid）/
    subject_id（前缀子树 LIKE）/ question_type / difficult。按 create_time 倒序。
    返回 [{id, questionType, difficult, subjectId, stemText, stemImg, status, labelStatus,
    examPaperName, createUser, importSource, importBatchId, createTime}]（camelCase 供 _map_item 复用）。
    🔴 只读，挂账同本模块头注释。"""
    conds, args = ["1=1"], []
    if batch_id:
        conds.append("import_batch_id=%s")
        args.append(str(batch_id))
    if since_dt is not None:
        conds.append("create_time>=%s")
        args.append(since_dt)
    if mine and uid is not None:
        conds.append("create_user=%s")
        args.append(int(uid))
    if subject_id:
        conds.append("CAST(subject_id AS CHAR) LIKE %s")
        args.append(str(subject_id) + "%")
    if question_type is not None:
        conds.append("question_type=%s")
        args.append(int(question_type))
    if difficult is not None:
        conds.append("difficult=%s")
        args.append(int(difficult))
    sql = ("SELECT CAST(id AS CHAR), question_type, difficult, CAST(subject_id AS CHAR),"
           " stem_text, stem_img_url, status, label_status, exam_paper_name, create_user,"
           " import_source, import_batch_id, create_time FROM biz_question"
           f" WHERE {' AND '.join(conds)} ORDER BY create_time DESC, id DESC LIMIT %s OFFSET %s")
    args += [int(limit), int(offset)]
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "questionType": r[1], "difficult": r[2], "subjectId": r[3],
            "stemText": r[4], "stemImg": r[5], "status": r[6], "labelStatus": r[7],
            "examPaperName": r[8], "createUser": r[9],
            "importSource": r[10], "importBatchId": r[11],
            "createTime": r[12].strftime("%Y-%m-%d %H:%M:%S") if r[12] else None,
        })
    return out


def recent_papers(uid, since_dt, limit=50):
    """当前 uid 时间窗内创建的卷（biz_paper.create_by=str(uid)，字符串 uid）。
    返回 [{id, name, question_count, create_time}]。只读。"""
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT CAST(id AS CHAR), name, question_count, create_time FROM biz_paper"
                " WHERE create_by=%s AND create_time>=%s ORDER BY create_time DESC LIMIT %s",
                (str(uid), since_dt, int(limit)))
            rows = cur.fetchall()
    finally:
        c.close()
    return [{"id": r[0], "name": r[1], "question_count": r[2],
             "create_time": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None} for r in rows]


def recent_lecture_frags(uid, since_dt, limit=50):
    """当前 uid 时间窗内录的讲义片段（biz_kg_lecture_frag.owner_id=uid，整型 uid）。
    该表有 owner_id + create_time 列，故支持时间窗找回。返回 [{id, title, create_time}]。只读。"""
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT CAST(id AS CHAR), title, create_time FROM biz_kg_lecture_frag"
                " WHERE owner_id=%s AND create_time>=%s ORDER BY create_time DESC LIMIT %s",
                (int(uid), since_dt, int(limit)))
            rows = cur.fetchall()
    finally:
        c.close()
    return [{"id": r[0], "title": r[1],
             "create_time": r[2].strftime("%Y-%m-%d %H:%M:%S") if r[2] else None} for r in rows]


# ───────────────────────── ⑥ 散题安全删除（v4 散题拍板 2026-07-29：孤题直接删）─────────────────────────
# 🔴 只能删"散题"：被任何载体/资产引用的题一律拒删（blocked 返回，要删先解绑）。
# 阻删引用面 = 四载体 + 解题模型母题 + 活血缘母题（variant 不在本批删除集）+ 讲义 doc_json qid。

_DEL_BLOCKERS = [
    ("biz_paper_question", "question_id", "试卷"),
    # biz_exam_paper_item（导入卷）2026-08-05 数据库清理批2 删表，随删剔除
    ("biz_shelf_item", "question_id", "书架书"),
    ("biz_book_question", "question_id", "教辅书"),
    ("biz_solution_model", "mother_question_id", "解题模型母题"),
]
_DEL_CASCADES = [
    "biz_question_ai", "biz_question_block", "biz_question_free_tag", "biz_question_image",
    "biz_question_knowledge", "biz_question_model", "biz_question_note", "biz_question_pattern_rel",
    "biz_question_pitfall", "biz_question_basket", "biz_question_favorite", "biz_review_issue",
    "biz_text_content",  # biz_anchor_worklist 2026-08-05 批2 删表，随删剔除
]


def delete_questions_safe(question_ids, actor="agent", reason="", dry_run=True, backup_dir=None):
    """散题安全删除。dry_run=True 只预览（deletable/blocked，不动库）；真删前备份主行+正文 JSON，
    随删全部附属表行 + variation_trace(variant 侧)，写 biz_label_audit 审计。单次 ≤500。
    返回 {ok, deletable, blocked{id:原因[]}, deleted, backup_path}。"""
    import json as _json
    import re as _re
    import tempfile
    from datetime import datetime as _dt

    ids = sorted({int(q) for q in question_ids})
    if not ids:
        return {"ok": False, "reason": "question_ids 为空"}
    if len(ids) > 500:
        return {"ok": False, "reason": f"单次上限 500，传了 {len(ids)}"}
    fmt = ",".join(["%s"] * len(ids))
    blocked = {}

    def _block(qid, why):
        blocked.setdefault(str(qid), []).append(why)

    c = conn()
    try:
        with c.cursor() as cur:
            # 只删真实存在的题
            cur.execute(f"SELECT id FROM biz_question WHERE id IN ({fmt})", ids)
            existing = {r[0] for r in cur.fetchall()}
            for q in ids:
                if q not in existing:
                    _block(q, "题不存在")
            # 载体/资产引用闸
            for table, col, label in _DEL_BLOCKERS:
                cur.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IN ({fmt})", ids)
                for (q,) in cur.fetchall():
                    _block(q, label)
            # 活血缘母题闸：作为母题且其变式不在本批删除集 → 拒删
            cur.execute(
                f"SELECT DISTINCT mother_question_id FROM biz_variation_trace"
                f" WHERE mother_question_id IN ({fmt}) AND variant_question_id NOT IN ({fmt})",
                ids + ids)
            for (q,) in cur.fetchall():
                _block(q, "活血缘母题")
            # 讲义闸：doc_json 内 qid 引用（讲义量小，全抽）
            cur.execute("SELECT doc_json FROM biz_kg_doc WHERE doc_json LIKE %s", ('%"qid"%',))
            lecture_qids = set()
            for (dj,) in cur.fetchall():
                for m in _re.finditer(r'"qid"\s*:\s*"?(\d{5,})', dj or ""):
                    lecture_qids.add(int(m.group(1)))
            for q in ids:
                if q in lecture_qids:
                    _block(q, "讲义引用")

            deletable = [q for q in ids if str(q) not in blocked]
            if dry_run:
                return {"ok": True, "dry_run": True, "deletable": [str(q) for q in deletable],
                        "deletable_count": len(deletable), "blocked": blocked}
            if not deletable:
                return {"ok": False, "reason": "全部被阻删", "blocked": blocked}

            dfmt = ",".join(["%s"] * len(deletable))
            # 备份：主行 + 正文
            cur.execute(f"SELECT * FROM biz_question WHERE id IN ({dfmt})", deletable)
            qcols = [d[0] for d in cur.description]
            qrows = [dict(zip(qcols, r)) for r in cur.fetchall()]
            cur.execute(f"SELECT * FROM biz_text_content WHERE question_id IN ({dfmt})", deletable)
            tcols = [d[0] for d in cur.description]
            trows = [dict(zip(tcols, r)) for r in cur.fetchall()]
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            bdir = backup_dir or tempfile.gettempdir()
            bpath = f"{bdir}/deleted_questions_{settings.db_database}_{ts}.json"
            with open(bpath, "w", encoding="utf-8") as f:
                _json.dump({"reason": reason, "actor": actor, "db": settings.db_database,
                            "questions": qrows, "text_contents": trows},
                           f, ensure_ascii=False, default=str)
            # 事务删除：附属 → 血缘(variant 侧) → 主表
            for table in _DEL_CASCADES:
                cur.execute(f"DELETE FROM {table} WHERE question_id IN ({dfmt})", deletable)
            cur.execute(f"DELETE FROM biz_variation_trace WHERE variant_question_id IN ({dfmt})", deletable)
            cur.execute(f"DELETE FROM biz_question WHERE id IN ({dfmt})", deletable)
            cur.execute(
                "INSERT INTO biz_label_audit(api,action,target_type,target_id,actor,actor_type,"
                "payload_brief,result) VALUES('delete_questions','DELETE','question',%s,%s,'agent',%s,'ok')",
                (str(len(deletable)), (actor or "agent")[:64],
                 _json.dumps({"reason": reason, "ids": [str(q) for q in deletable[:60]],
                              "total": len(deletable)}, ensure_ascii=False)[:2000]))
            c.commit()
            return {"ok": True, "dry_run": False, "deleted": [str(q) for q in deletable],
                    "deleted_count": len(deletable), "blocked": blocked, "backup_path": bpath}
    finally:
        c.close()


# ───────────────────────── ④ KG 只读查表 ─────────────────────────

def _subseq(query, name):
    """query 的字符是否按序散布于 name（「有理数乘法」⊂「有理数的乘法法则」）。LIKE 零命中时的兜底匹配。"""
    it = iter(name)
    return all(ch in it for ch in query)


def kg_query(subject_root, query="", section_num="", parent_id="", leaves_only=False, limit=50):
    """biz_subject 只读查表（resolve_kg 底座）。
    🔴 叶子=无子节点（H2 实测：901 树 5 层、902-906 树 4 层，叶深不一，不得写死 level）。
    query 先 LIKE 精配；**最终结果为空**（含 leaves_only 过滤后）自动退子序列匹配并合并
    （治「有理数乘法」LIKE 只命中非叶「…乘法的运算律」、真叶「有理数的乘法法则」漏配——AC7 反馈）。
    返回 [{id,name,level,parent_id,is_leaf}]。"""
    import re

    def _filter_build(cur, rows):
        if section_num:
            rows = [r for r in rows if (m := re.match(r"^\s*(\d+\.\d+)\s+", r[1])) and m.group(1) == section_num]
        ids = [r[0] for r in rows]
        has_child = set()
        if ids:
            fmt = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT DISTINCT CAST(parent_id AS CHAR) FROM biz_subject WHERE CAST(parent_id AS CHAR) IN ({fmt})", ids)
            has_child = {r[0] for r in cur.fetchall()}
        out = [{"id": i, "name": n, "level": lv, "parent_id": p, "is_leaf": i not in has_child}
               for i, n, lv, p in rows]
        return [x for x in out if x["is_leaf"]] if leaves_only else out

    c = conn()
    with c.cursor() as cur:
        conds, args = ["CAST(id AS CHAR) LIKE %s"], [str(subject_root) + "%"]
        if parent_id:
            conds, args = ["parent_id=%s"], [str(parent_id)]
        if query:
            conds.append("name LIKE %s")
            args.append(f"%{query}%")
        cur.execute(
            "SELECT CAST(id AS CHAR), name, level, CAST(parent_id AS CHAR) FROM biz_subject"
            f" WHERE {' AND '.join(conds)} ORDER BY id LIMIT %s", (*args, int(limit) * 4))
        out = _filter_build(cur, cur.fetchall())
        if not out and query:
            # LIKE 路线（含 leaves_only 过滤后）为空 → 拉全根子序列兜底（单树几百节点，内存筛便宜且确定性）
            base_conds = ["parent_id=%s"] if parent_id else ["CAST(id AS CHAR) LIKE %s"]
            base_args = [str(parent_id)] if parent_id else [str(subject_root) + "%"]
            cur.execute(
                "SELECT CAST(id AS CHAR), name, level, CAST(parent_id AS CHAR) FROM biz_subject"
                f" WHERE {' AND '.join(base_conds)} ORDER BY id", base_args)
            rows = [r for r in cur.fetchall() if _subseq(query, r[1])][: int(limit) * 4]
            out = _filter_build(cur, rows)
    c.close()
    return out[: int(limit)]
