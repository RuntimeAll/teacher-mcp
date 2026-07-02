"""同步练习卷·轻管线录入（机械录入 + 确定性 KG 锚定，核心 AI 打标另走 apply_light_dna）。

与日常考试全 DNA 管线(run_paper→label_runner)的区别：同步练习是**单知识点课后作业**，知识点锚定
**确定性**——文件名「X.Y 节名#N」直接命中 KG level-3 节点（无需 LLM 分类/白名单/防串）。故：
  ① 全卷题目 subject_id + dim1_kp_id = 该节点（一个卷一个节点）；
  ② 卷绑「同步练习」分类树的章节点（可浏览目录）；
  ③ 题型/分值/答案/解析 = parser 机械抽（ingest_paper 复用）；
  ④ 核心 AI 打标（难度★ + 易错）= agent 读 digest 出极小 json → apply_light_dna（不在此步）。

用法（cwd=teacher-mcp）:
  # 预飞行（不写库，只出映射+parse+digest）
  .venv\\Scripts\\python.exe tools\\sync_ingest.py --doc "<文件>" --root-id 100 --batch QS7U --dry
  # 真录入（机械录入 + 写 KG 关系）
  .venv\\Scripts\\python.exe tools\\sync_ingest.py --doc "<文件>" --root-id 100 --batch QS7U
接着 agent 据 digest 写 {num:[难度,易错...]} → tools\\sync_label.py --paper-id NN --dna <json>
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
WORK = ROOT / ".paper_work"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.ingest_paper import parse_paper, plain_text  # noqa: E402
from tools.dbutil import conn, resolve_qids, write_relations  # noqa: E402

# 同步练习分类树根（新建，与 3001公共/3003资料库/3004专题 并列，本身即「类型=同步练习」）
SYNC_CAT_ROOT = "3005"
# root_id(KG 教材根) → (册序号, 册名, subject, stage, grade, volume) —— 结构列镜像现有分类惯例
# subject=1数学 stage=2初中 grade=年级 volume=1上/2下
BOOK_META = {
    "100": (1, "七年级上册", 1, 2, 7, 1), "401": (2, "七年级下册", 1, 2, 7, 2),
    # 后续 八上/八下/九上/九下 用到再补（对应各自 KG 根）
}


def parse_fname(doc):
    """文件名「X.Y 节名#N.doc」→ (sec_num='X.Y', title, seq)。"""
    fn = os.path.basename(doc).replace(".doc", "").replace(".docx", "")
    m = re.match(r"^\s*(\d+\.\d+)\s+(.+?)#(\d+)\s*$", fn)
    if not m:
        raise ValueError(f"文件名不规范（应为『X.Y 节名#N』）: {fn}")
    return m.group(1), m.group(2).strip(), m.group(3)


def resolve_node(root_id, sec_num):
    """KG 里查节名前缀=sec_num 的 level-3 节点 → (节点id, 节名, 章节点6位)。"""
    c = conn()
    with c.cursor() as cur:
        cur.execute("SELECT CAST(id AS CHAR), name FROM biz_subject "
                    "WHERE CAST(id AS CHAR) LIKE %s AND level=3", (root_id + "%",))
        for nid, name in cur.fetchall():
            mm = re.match(r"^\s*(\d+\.\d+)\s+", name)
            if mm and mm.group(1) == sec_num:
                c.close()
                return nid, name, nid[:6]
    c.close()
    raise ValueError(f"KG 无匹配节点: 教材根{root_id} 节{sec_num}")


def ensure_category(root_id, chap_id):
    """确保同步练习分类树存在 该册→该章 节点，返回章分类节点 id。幂等。
    结构: 3005 同步练习(root) → 3005{册:03d} 册名(grade) → 3005{册:03d}{章:03d} 章名(chapter)。
    结构列 subject/stage/grade/volume 镜像现有分类惯例（kg-enums 字典化后 FE 靠这些列筛/渲染）。"""
    book_idx, book_name, subj, stage, grade, vol = BOOK_META[root_id]
    book_cat = f"{SYNC_CAT_ROOT}{book_idx:03d}"
    chap_idx = int(chap_id[3:])                       # 100003 → 3
    chap_cat = f"{book_cat}{chap_idx:03d}"
    c = conn()
    with c.cursor() as cur:
        cur.execute("SELECT name FROM biz_subject WHERE CAST(id AS CHAR)=%s", (chap_id,))
        row = cur.fetchone()
        chap_name = row[0] if row else f"第{chap_idx}章"
        # 幂等建三级：(id, parent, name, sort, node_kind, 结构列是否带年级维)
        rows = [
            (SYNC_CAT_ROOT, "0", "同步练习", 5, "root", False),
            (book_cat, SYNC_CAT_ROOT, book_name, book_idx, "grade", True),
            (chap_cat, book_cat, chap_name, chap_idx, "chapter", True),
        ]
        for cid, pid, nm, srt, kind, dims in rows:
            cur.execute("SELECT 1 FROM biz_paper_category WHERE CAST(id AS CHAR)=%s", (cid,))
            if cur.fetchone():
                continue
            if dims:
                cur.execute("INSERT INTO biz_paper_category(id,parent_id,name,sort,subject,stage,grade,volume,node_kind)"
                            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (cid, pid, nm, srt, subj, stage, grade, vol, kind))
            else:
                cur.execute("INSERT INTO biz_paper_category(id,parent_id,name,sort,node_kind)"
                            " VALUES(%s,%s,%s,%s,%s)", (cid, pid, nm, srt, kind))
        c.commit()
    c.close()
    return chap_cat, chap_name


def make_digest(qs):
    # 逻辑上提 app/paperparse.make_digest（PRD-C-208；90 字轻管线口径），位置序号=DB sort 约定不变
    from app.paperparse import make_digest as _md
    return _md(qs, stem_width=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--root-id", required=True, help="KG 教材根(七上=100)")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--dry", action="store_true", help="预飞行：只映射+parse+digest，不写库")
    a = ap.parse_args()

    sec_num, title, seq = parse_fname(a.doc)
    node_id, node_name, chap_id = resolve_node(a.root_id, sec_num)
    paper_name = f"{node_name} 课后作业#{seq}"
    print(f"[map] {sec_num}#{seq} 「{title}」 → KG节点 {node_id} [{node_name}] · 章 {chap_id}")

    WORK.mkdir(exist_ok=True)
    txt = WORK / f"{a.batch}.txt"
    r = subprocess.run([PY, str(ROOT / "tools" / "omml_to_text.py"), a.doc, str(txt)],
                       capture_output=True, text=True, encoding="utf-8")
    print(f"[convert] {r.stdout.strip()}")
    if r.returncode != 0:
        print(r.stderr[-400:]); return

    qs = parse_paper(txt.read_text(encoding="utf-8"))
    figs = sum(1 for q in qs if q["has_fig"])
    ans = sum(1 for q in qs if q["answer"])
    print(f"[parse] {len(qs)} 题（含图 {figs} · 有答案 {ans}）")

    if a.dry:
        print(f"[dry] 卷名将为「{paper_name}」，全部题目锚 dim1_kp_id={node_id}，分类将建于同步练习树")
        print(f"\n[digest] {len(qs)} 题\n{make_digest(qs)}")
        return

    chap_cat, chap_name = ensure_category(a.root_id, chap_id)
    print(f"[cat] 分类节点 {chap_cat} [同步练习/{BOOK_META[a.root_id][1]}/{chap_name}]")

    # 机械录入（题目 subject_id=节点；建卷绑分类；题量*5 通值分、40min）
    total = max(len(qs) * 5, 20)
    cmd = [PY, str(ROOT / "tools" / "ingest_paper.py"), "--txt", str(txt), "--docx", a.doc,
           "--subject-id", node_id, "--batch", a.batch, "--category-id", chap_cat,
           "--paper-name", paper_name, "--total-score", str(total), "--suggest-time", "40",
           "--source-type", "6"]  # 6=自编/同步（教辅同步练习）
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    for ln in (r.stdout or "").splitlines():
        if ln.startswith(("[parse]", "[done]", "[paper]", "  ♻", "  ❌")):
            print(ln)
    m = re.search(r"paper_id=(\d+)", r.stdout or "")
    paper_id = m.group(1) if m else None
    if not paper_id:
        print("[err] 未拿到 paper_id"); print((r.stderr or "")[-600:]); return

    # 确定性写 KG 关系（全卷题目 dim1_kp_id=节点，零 LLM）+ 同步 subject_id
    qmap = resolve_qids(paper_id)
    recs = [{"question_id": qid, "dim1_kp_id": node_id} for qid in qmap.values()]
    kg_n, _ = write_relations(recs)
    # dim1_kp_id 列也补（write_relations 只写关系表+subject_id；summarize 读 dim1_kp_id 列）
    c = conn()
    with c.cursor() as cur:
        for qid in qmap.values():
            cur.execute("UPDATE biz_question SET dim1_kp_id=%s WHERE id=%s", (node_id, int(qid)))
        c.commit()
    c.close()
    print(f"[kg] 锚定 {len(qmap)} 题 → {node_id}（新增关系 {kg_n}）")
    print(f"[paper] ✅ paper_id={paper_id}")
    print(f"\n[digest] {len(qs)} 题（据此写难度）\n{make_digest(qs)}")
    print(f"\n下一步：agent 出 {{num:[难度1-4,易错]}} → sync_label.py --paper-id {paper_id} --dna <json>")


if __name__ == "__main__":
    main()
