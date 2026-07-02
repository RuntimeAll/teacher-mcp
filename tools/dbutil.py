"""dev 库(ai_lesson_prep @ :3307)关系表 pymysql 工具 —— 给本地拆题/打标管线复用。

🔴 范式说明：MCP server(app/) 保持纯 HTTP（架构铁律：Python 不直连 RuoYi MySQL）。
   这里是 **tools/ 下的本地管线脚本**（与 ingest_paper._set_paper_subject 同类），
   做 label_question(HTTP) 写不了的关系表：biz_question_knowledge(知识点) / biz_question_model(模型链)，
   抽新模型(propose_models) / 总评(summarize_paper) / 重跑(delete_paper)。仅 dev 本地凭据、增量幂等，不进 MCP server 层。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.dicts import QUESTION_TYPE, QUESTION_DIFFICULTY  # noqa: E402  题型/难度码 SSOT
# 🔴 PRD-C-208：连接/新模型提议上提 app/db.py（settings 凭据 + propose 串行），此处 re-export 保持脚本 API 不变
from app.db import conn, propose_models as _app_propose_models  # noqa: E402,F401

LOGFILE = ROOT / ".paper_run.log"   # MCP server / httpx 噪声日志落这（控制台干净，省 token）


def errlog():
    """stdio_client 的 errlog：把子进程 stderr(Processing request/HTTP Request 噪声)引到文件而非控制台。"""
    return open(LOGFILE, "a", encoding="utf-8")


def resolve_qids(paper_id):
    """{题号 sort: question_id}（pymysql 读，bigint 精确不截断）。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("SELECT sort, question_id FROM biz_paper_question WHERE paper_id=%s", (int(paper_id),))
        m = {int(s): int(q) for s, q in cur.fetchall()}
    c.close()
    return m


def write_relations(records):
    """逐题写 biz_question_knowledge(主 dim1_kp_id is_primary=1 + 可选 secondary_kps) + biz_question_model(models[])。
    records 每条需含已解析的 question_id；增量幂等（按 q+kp / q+model 去重）。返回 (知识点新增, 模型链新增)。"""
    c = conn()
    kg_n = mdl_n = 0
    with c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id),0) FROM biz_question_knowledge")
        kbase = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(MAX(id),0) FROM biz_question_model")
        mbase = cur.fetchone()[0]
        for r in records:
            qid = int(r["question_id"])
            kps = []
            if r.get("dim1_kp_id"):
                kps.append((r["dim1_kp_id"], 1))
                # 🔴 subject_id(章节字段) 跟 dim1_kp_id(主考点) 平行同步 —— 录入时 subject_id 被设成 KG 根，
                #    打标后细化到真锚,消除「知识点=角的和差 / 章节=根」的不一致 + 让题库目录按前缀正确归类。
                #    (schema 收敛前的过渡方案:两字段并存但保持一致;只同步有锚的题,无锚题留原 subject_id。)
                cur.execute("UPDATE biz_question SET subject_id=%s WHERE id=%s", (str(r["dim1_kp_id"]), qid))
            for s in r.get("secondary_kps", []) or []:
                kps.append((s, 0))
            for kp, prim in kps:
                cur.execute("SELECT 1 FROM biz_question_knowledge WHERE question_id=%s AND knowledge_id=%s LIMIT 1", (qid, kp))
                if cur.fetchone():
                    continue
                kbase += 1
                cur.execute("INSERT INTO biz_question_knowledge(id,question_id,knowledge_id,source,is_primary,create_time)"
                            " VALUES(%s,%s,%s,'U',%s,NOW())", (kbase, qid, kp, prim))
                kg_n += 1
            for m in r.get("models", []) or []:
                mid, prim = m["model_id"], int(m.get("is_primary", 0))
                cur.execute("SELECT 1 FROM biz_question_model WHERE question_id=%s AND model_id=%s LIMIT 1", (qid, mid))
                if cur.fetchone():
                    continue
                mbase += 1
                cur.execute("INSERT INTO biz_question_model(id,question_id,model_id,is_primary,source,role)"
                            " VALUES(%s,%s,%s,%s,'AI',%s)", (mbase, qid, mid, prim, "主" if prim else "辅"))
                mdl_n += 1
        c.commit()
    c.close()
    return kg_n, mdl_n


def propose_models(records):
    """🔴 抽新模型：DNA 里每题 new_models[]（现有模型覆盖不了的难题招式）建档入 biz_solution_model。
    逻辑上提 app/db.propose_models（PRD-C-208，与 ingest_items 同源、串行防撞 TY 主键），此处委托。
    模型⟺难度铁律：★3/★4 必有模型，无现成即在此抽取；★2 可有；★1 不需。"""
    return _app_propose_models(records)


def model_gaps(paper_id):
    """模型⟺难度闸：返回 ★3+ 但库里零模型链的题号列表（应抽模型却漏的题）。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("""SELECT pq.sort FROM biz_paper_question pq JOIN biz_question q ON q.id=pq.question_id
                       WHERE pq.paper_id=%s AND q.difficult>=3
                         AND NOT EXISTS(SELECT 1 FROM biz_question_model m WHERE m.question_id=q.id)
                       ORDER BY pq.sort""", (int(paper_id),))
        gaps = [int(r[0]) for r in cur.fetchall()]
    c.close()
    return gaps


def summarize_paper(paper_id, write_md=True):
    """🔴 整卷总评（纯 DB 聚合，零 LLM token）：题量/题型/难度/章节考频/模型覆盖/重难点清单。
    返回 markdown 字符串；write_md=True 时落 .paper_summary_<id>.md。"""
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT name,score,suggest_time,question_count FROM biz_paper WHERE id=%s", (int(paper_id),))
    pname, tscore, stime, qcount = cur.fetchone()
    cur.execute("""SELECT pq.sort,pq.score,q.question_type,q.difficult,q.dim1_kp_id,CAST(q.id AS CHAR)
                   FROM biz_paper_question pq JOIN biz_question q ON q.id=pq.question_id
                   WHERE pq.paper_id=%s ORDER BY pq.sort""", (int(paper_id),))
    qs = cur.fetchall()
    qids = [r[5] for r in qs]
    mods, brk = {}, {}
    if qids:
        fmt = ",".join(["%s"] * len(qids))
        cur.execute(f"""SELECT m.question_id,sm.name,sm.status FROM biz_question_model m
                        JOIN biz_solution_model sm ON sm.id=m.model_id WHERE m.question_id IN ({fmt})""", qids)
        for qid, mname, st in cur.fetchall():
            mods.setdefault(str(qid), []).append((mname, st))
        cur.execute(f"SELECT question_id,breakthrough_points FROM biz_question_ai WHERE question_id IN ({fmt})", qids)
        for qid, bp in cur.fetchall():
            brk[str(qid)] = bp or ""
    cur.execute("SELECT CAST(id AS CHAR),name FROM biz_subject WHERE CHAR_LENGTH(CAST(id AS CHAR))=6")
    chap = {cid: name for cid, name in cur.fetchall()}
    c.close()

    from collections import defaultdict
    tdist, ddist = defaultdict(int), defaultdict(int)
    cscore, ccount = defaultdict(float), defaultdict(int)
    hards, used_models, new_models = [], set(), set()
    covered3 = 0
    n3 = 0
    for sort, score, qtype, diff, kp, qid in qs:
        tdist[int(qtype)] += 1
        ddist[int(diff or 0)] += 1
        ck = (str(kp) or "")[:6]
        cscore[ck] += float(score or 0)
        ccount[ck] += 1
        ms = mods.get(str(qid), [])
        for mn, st in ms:
            used_models.add(mn)
            if st == "2":
                new_models.add(mn)
        if int(diff or 0) >= 3:
            n3 += 1
            if ms:
                covered3 += 1
            import json as _j
            try:
                bp = "；".join(_j.loads(brk.get(str(qid)) or "[]"))
            except Exception:
                bp = brk.get(str(qid), "")
            hards.append((sort, int(diff), chap.get(ck, ck), [m for m, _ in ms], bp))

    L = []
    L.append(f"# 试卷总评 · {pname}")
    L.append(f"- **规模**：{qcount} 题 / 总分 {int(tscore or 0)} / 建议 {int(stime or 0)} 分钟")
    L.append(f"- **题型**：" + "，".join(f"{QUESTION_TYPE.get(t,t)}{n}" for t, n in sorted(tdist.items())))
    L.append(f"- **难度**：" + "，".join(f"★{d}×{ddist[d]}" for d in sorted(ddist)) +
             f"（★3+ 占比 {round(100*(ddist[3]+ddist[4])/max(qcount,1))}%）")
    L.append("\n## 章节考频（按分值占比）")
    tot = sum(cscore.values()) or 1
    for ck in sorted(cscore, key=lambda k: -cscore[k]):
        L.append(f"- {chap.get(ck,ck)}：{ccount[ck]}题 / {int(cscore[ck])}分（{round(100*cscore[ck]/tot)}%）")
    L.append("\n## 模型覆盖")
    L.append(f"- ★3+ 题 {n3} 道，已挂模型 {covered3} 道（覆盖 {round(100*covered3/max(n3,1))}%）")
    L.append(f"- 用到模型 {len(used_models)} 种：" + ("，".join(sorted(used_models)) or "无"))
    if new_models:
        L.append(f"- 🆕 本卷抽取新模型 {len(new_models)} 个（status=2 待转正）：" + "，".join(sorted(new_models)))
    L.append("\n## 重难点清单（★3/★4）")
    for sort, diff, cname, ms, bp in sorted(hards, key=lambda x: (-x[1], x[0])):
        L.append(f"- **第{sort}题 ★{diff}** [{cname}] 模型：{('/'.join(ms) or '⚠无')}　突破口：{bp[:60]}")
    md = "\n".join(L)
    if write_md:
        (ROOT / f".paper_summary_{paper_id}.md").write_text(md, encoding="utf-8")
    return md


def set_paper_review(paper_id, review_md):
    """把 LLM 总结语(教师视角定性总评)写进 biz_paper.remark —— 老师在试卷详情即可看。
    与 summarize_paper(机器统计)互补：那份是骨架,这份是 agent 读透全卷后的判断。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("UPDATE biz_paper SET remark=%s WHERE id=%s", (review_md, int(paper_id)))
        c.commit()
    c.close()


def delete_paper(paper_id):
    """重跑用：删一套卷 + 其题的全部关系（biz_question 及 ai/knowledge/model/image/block/text + paper 三表）。返回删题数。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("SELECT question_id FROM biz_paper_question WHERE paper_id=%s", (int(paper_id),))
        qids = [int(r[0]) for r in cur.fetchall()]
        if qids:
            fmt = ",".join(["%s"] * len(qids))
            for t in ("biz_question_ai", "biz_question_knowledge", "biz_question_model",
                      "biz_question_image", "biz_question_block", "biz_text_content", "biz_question_free_tag"):
                try:
                    cur.execute(f"DELETE FROM {t} WHERE question_id IN ({fmt})", qids)
                except Exception as e:
                    print(f"  (跳过 {t}: {type(e).__name__})")
            cur.execute(f"DELETE FROM biz_question WHERE id IN ({fmt})", qids)
        cur.execute("DELETE FROM biz_paper_question WHERE paper_id=%s", (int(paper_id),))
        cur.execute("DELETE FROM biz_paper_section WHERE paper_id=%s", (int(paper_id),))
        cur.execute("DELETE FROM biz_paper WHERE id=%s", (int(paper_id),))
        c.commit()
    c.close()
    return len(qids)
