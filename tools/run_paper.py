"""一条龙录入编排（省 token）：转换 omml→text + 录入 ingest_paper + 出**题面摘要 digest**。
原本要 3 次 bash 往返 + Claude 读 357 行全文；现在 1 条命令搞定，Claude 只读 ~24 行紧凑 digest 即可写 DNA。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\run_paper.py --doc 原卷.doc --subject-id 100 --category-id 3001004004 --batch JX-2026

产物：① .paper_work/<batch>.txt 原文（DNA 细节兜底）② 控制台 [paper] paper_id ③ digest（每题 1 行：题号/题型/分值/有图/题干/选项/答案）直接打印，Claude 据此产 DNA json。
打标接着走 label_runner.py --dna <json> --paper-id <NN>（已含抽模型 + 总评）。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
WORK = ROOT / ".paper_work"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.ingest_paper import parse_paper, plain_text  # noqa: E402


def make_digest(txt_path):
    qs = parse_paper(Path(txt_path).read_text(encoding="utf-8"))
    out = []
    # 🔴 用位置序号 i(1..N) 当题号 = DB biz_paper_question.sort（入库按 parse 顺序编 sort）。
    #    不用 q["num"]（parser 从原卷抓的题号，遇卷内杂散「N.」会错标/重号，致打标 num→sort 错位）。
    for i, q in enumerate(qs, 1):
        opt = "  [" + " | ".join(q["options"]) + "]" if q["options"] else ""
        stem = plain_text(q["stem"]).replace("\n", " ")
        fig = "🖼" if q["has_fig"] else "  "
        out.append(f'#{i:>2} t{q["type"]}{fig}{int(q["score"])}分 | {stem}{opt}  =答:{q["answer"][:50]}')
    return qs, "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--subject-id", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--category-id", default="")
    ap.add_argument("--paper-name", default="")
    ap.add_argument("--total-score", default="120")
    ap.add_argument("--suggest-time", default="120")
    ap.add_argument("--region-code", default="")
    ap.add_argument("--source-type", default="0")
    ap.add_argument("--exam-year", default="")
    ap.add_argument("--no-ingest", action="store_true", help="只转换+摘要不录入(预检)")
    a = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    txt = WORK / f"{a.batch}.txt"
    # ① 转换
    r = subprocess.run([PY, str(ROOT / "tools" / "omml_to_text.py"), a.doc, str(txt)],
                       capture_output=True, text=True, encoding="utf-8")
    print(f"[convert] {r.stdout.strip()}")
    if r.returncode != 0:
        print(r.stderr[-500:])
        return

    paper_id = None
    if not a.no_ingest:
        # ② 录入（ingest_paper 自己把 MCP 噪声引 .paper_run.log，控制台只剩 [parse]/[done]/[paper]）
        cmd = [PY, str(ROOT / "tools" / "ingest_paper.py"), "--txt", str(txt), "--docx", a.doc,
               "--subject-id", a.subject_id, "--batch", a.batch, "--category-id", a.category_id,
               "--total-score", a.total_score, "--suggest-time", a.suggest_time,
               "--region-code", a.region_code, "--source-type", a.source_type, "--exam-year", a.exam_year]
        if a.paper_name:
            cmd += ["--paper-name", a.paper_name]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        for ln in (r.stdout or "").splitlines():
            if ln.startswith(("[parse]", "[金标]", "[done]", "[paper]", "  ♻", "  ❌")):
                print(ln)
        m = re.search(r"paper_id=(\d+)", r.stdout or "")
        paper_id = m.group(1) if m else None
        if r.returncode != 0 and not paper_id:
            print(r.stderr[-800:])

    # ③ 摘要
    qs, dg = make_digest(txt)
    print(f"\n[digest] {len(qs)} 题（txt={txt}）" + (f" · paper_id={paper_id}" if paper_id else ""))
    print(dg)
    if paper_id:
        print(f"\n下一步：写 DNA json（按题号 num，★3+必带 models/new_models）→ "
              f"label_runner.py --dna <json> --paper-id {paper_id}")


if __name__ == "__main__":
    main()
