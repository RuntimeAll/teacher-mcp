"""同步练习·核心 AI 打标应用（难度★ + 易错，轻量）。

agent 读 sync_ingest 出的 digest → 写极小 DNA json（每题只判 难度1-4 + 易错点，不做模型/血缘/解法）
→ 本脚本 apply：UPDATE biz_question.difficult + upsert biz_question_ai(breakthrough_points/difficulty_reason)。
知识点/题型/分值/答案已在 sync_ingest 机械落库，这里只补 parser 给不了的「难度 + 易错」。

DNA json 格式（键=题号，按 digest 的 #num）:
  {"1": {"d": 1, "err": ["计算失误"], "why": "一步直算"},
   "5": {"d": 3, "err": ["分类不全","隐含遗漏"], "why": "需分正负两类讨论"}}
  d=难度 1基础/2中等/3较难/4压轴；err=易错点(可空)；why=难度一句依据(可空)。

用法: .venv\\Scripts\\python.exe tools\\sync_label.py --paper-id NN --dna dna.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.dbutil import conn, resolve_qids  # noqa: E402


def apply_light_dna(paper_id, dna):
    """dna: {题号str: {d, err[], why}} → 应用难度+易错。返回 (改难度数, 写易错数)。"""
    qmap = resolve_qids(paper_id)     # {sort: qid}
    c = conn()
    dn = an = 0
    with c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id),0) FROM biz_question_ai")
        aibase = cur.fetchone()[0]
        for num_s, v in dna.items():
            qid = qmap.get(int(num_s))
            if not qid:
                print(f"  ⚠ 题号 {num_s} 无对应题（跳过）"); continue
            d = int(v.get("d", 2))
            cur.execute("UPDATE biz_question SET difficult=%s WHERE id=%s", (d, int(qid)))
            dn += 1
            err = v.get("err") or []
            why = (v.get("why") or "")[:500]
            if not err and not why:
                continue
            bp = json.dumps(err, ensure_ascii=False)
            cur.execute("SELECT id FROM biz_question_ai WHERE question_id=%s LIMIT 1", (int(qid),))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE biz_question_ai SET breakthrough_points=%s, difficulty_reason=%s,"
                            " label_status=1, labeled_by='sync-light', labeled_at=NOW() WHERE id=%s",
                            (bp, why, row[0]))
            else:
                aibase += 1
                cur.execute("INSERT INTO biz_question_ai(id,question_id,annotate_version,breakthrough_points,"
                            "difficulty_reason,label_status,labeled_by,labeled_at,create_time)"
                            " VALUES(%s,%s,1,%s,%s,1,'sync-light',NOW(),NOW())",
                            (aibase, int(qid), bp, why))
            an += 1
        c.commit()
    c.close()
    return dn, an


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-id", required=True)
    ap.add_argument("--dna", required=True, help="DNA json 文件路径")
    a = ap.parse_args()
    dna = json.loads(Path(a.dna).read_text(encoding="utf-8"))
    dn, an = apply_light_dna(a.paper_id, dna)
    print(f"[light-dna] paper_id={a.paper_id} 改难度 {dn} 题，写易错/依据 {an} 题")


if __name__ == "__main__":
    main()
