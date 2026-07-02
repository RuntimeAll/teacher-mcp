"""日常考试·批量机械录入（跨知识点整卷，走全 DNA 前的 ingest 步）。
按卷名关键词映射试卷分类（期末/月考/期中/单元），subject-id=教材根(占位，DNA 阶段再细化每题锚)。
跳过卷名含 skip 关键词的（已录过的，防重）。digest 存 .paper_work/digests_ri/<paper_id>.txt 供全 DNA 打标。

用法: .venv\\Scripts\\python.exe tools\\ri_batch.py --dir "<日常考试/七上>" --root-id 100 --prefix RI7U --skip 启正
"""
import argparse, glob, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
WORK = ROOT / ".paper_work"
DIGDIR = WORK / "digests_ri"
PROG = WORK / "ri_batch.progress"

# 七上试卷分类（公共试卷/七年级上册/…）关键词→分类节点
CAT_MAP = [("期末", "3001004004"), ("月考", "3001004002"), ("期中", "3001004003"),
           ("单元", "3001004001"), ("章末", "3001004001")]


def cat_of(name):
    for kw, cid in CAT_MAP:
        if kw in name:
            return cid, kw
    return "3001004", "未分类"  # 兜底挂七上根


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--root-id", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--skip", default="")
    a = ap.parse_args()
    DIGDIR.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.dir, "*.doc")))
    prog = open(PROG, "a", encoding="utf-8")

    def log(s):
        print(s); prog.write(s + "\n"); prog.flush()

    log(f"\n===== 日常考试批量 {len(files)} 份（skip={a.skip}）=====")
    done = 0
    for i, f in enumerate(files):
        base = os.path.basename(f).replace(".doc", "")
        if a.skip and a.skip in base:
            log(f"⏭[{i:02d}] {base[:30]} —— 命中 skip，跳过（已录）"); continue
        cat, kw = cat_of(base)
        batch = f"{a.prefix}-{i:02d}"
        cmd = [PY, str(ROOT / "tools" / "run_paper.py"), "--doc", f, "--subject-id", a.root_id,
               "--batch", batch, "--category-id", cat, "--total-score", "120", "--suggest-time", "120"]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = r.stdout or ""
        if "login 失败" in out or "All connection attempts failed" in (r.stderr or ""):
            log(f"❌[{i:02d}] {base[:30]} 服务掉线，中止！"); break
        pid = (re.search(r"paper_id=(\d+)", out) or [None, None])[1]
        # 存 digest
        di = out.find("[digest]")
        if di >= 0 and pid:
            lines = out[di:].splitlines()[1:]
            dig = "\n".join(l for l in lines if l.startswith("#"))
            (DIGDIR / f"{pid}.txt").write_text(f"# {base}  cat={cat}({kw})  paper_id={pid}\n{dig}", encoding="utf-8")
        # 抓建卷行
        pl = next((l for l in out.splitlines() if l.startswith("[paper]")), "")
        log(f"{'✅' if pid else '⚠'}[{i:02d}] pid={pid} cat={kw} {base[:34]}  {pl[:60]}")
        if pid:
            done += 1
    log(f"\n===== 日常完成 {done} 份 · digests → {DIGDIR} =====")
    prog.close()


if __name__ == "__main__":
    main()
