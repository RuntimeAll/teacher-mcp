"""同步练习·批量轻录入驱动：循环一个目录下所有「X.Y 节名#N.doc」→ 逐份 sync_ingest（机械录入+KG锚定），
每份 digest 存 .paper_work/digests/<paper_id>.txt（供 phase2 打难度），进度写 .paper_work/sync_batch.progress。
服务掉线（login 失败）或连续 3 份失败即中止（不盲跑）。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\sync_batch.py --dir "<同步练习/七上>" --root-id 100 --prefix QS7U
产物：progress 文件（人读）+ digests/ 目录 + 末尾打印汇总（总题/去重复用/失败/图）。
"""
import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
WORK = ROOT / ".paper_work"
DIGDIR = WORK / "digests"
PROG = WORK / "sync_batch.progress"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--root-id", required=True)
    ap.add_argument("--prefix", required=True, help="batch 前缀，如 QS7U")
    ap.add_argument("--start", type=int, default=0, help="从第 N 份开始（断点续跑）")
    a = ap.parse_args()

    DIGDIR.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.dir, "*.doc")))
    files = files[a.start:]
    prog = open(PROG, "a", encoding="utf-8")

    def log(s):
        print(s)
        prog.write(s + "\n"); prog.flush()

    log(f"\n===== 批量开跑 {len(files)} 份（root={a.root_id} prefix={a.prefix} start={a.start}）=====")
    tot_q = tot_reuse = tot_fail = tot_img = done = 0
    consec_fail = 0
    for i, f in enumerate(files, start=a.start):
        base = os.path.basename(f).replace(".doc", "")
        batch = f"{a.prefix}-{i:02d}"
        r = subprocess.run(
            [PY, str(ROOT / "tools" / "sync_ingest.py"), "--doc", f,
             "--root-id", a.root_id, "--batch", batch],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = r.stdout or ""
        # 服务掉线检测
        if "login 失败" in out or "All connection attempts failed" in (r.stderr or "") or "未拿到 paper_id" in out:
            log(f"❌[{i:02d}] {base} —— 服务疑似掉线/录入失败，中止！(stderr尾: {(r.stderr or '')[-160:]})")
            break
        pid = (re.search(r"paper_id=(\d+)", out) or [None, None])[1]
        mdone = re.search(r"ok=(\d+)\(去重复用(\d+)\) fail=(\d+) total=(\d+) 传图=(\d+)", out)
        mkg = re.search(r"锚定 (\d+) 题", out)
        if not pid or not mdone:
            consec_fail += 1
            log(f"⚠[{i:02d}] {base} —— 无 paper_id/统计，可能解析异常 consec_fail={consec_fail}")
            if consec_fail >= 3:
                log("❌ 连续 3 份异常，中止！"); break
            continue
        consec_fail = 0
        ok, reuse, fail, total, img = map(int, mdone.groups())
        anchored = int(mkg.group(1)) if mkg else 0
        tot_q += ok; tot_reuse += reuse; tot_fail += fail; tot_img += img; done += 1
        # 存 digest（[digest] 之后到结尾的题行）
        di = out.find("[digest]")
        if di >= 0 and pid:
            lines = out[di:].splitlines()[1:]
            dig = "\n".join(l for l in lines if l.startswith("#"))
            (DIGDIR / f"{pid}.txt").write_text(f"# {base}  paper_id={pid}\n{dig}", encoding="utf-8")
        log(f"✅[{i:02d}] pid={pid} {base}  录{ok}(复用{reuse}) 锚{anchored} 图{img} 失{fail}")
    log(f"\n===== 完成 {done} 份 · 录入题次 {tot_q}（去重复用 {tot_reuse}）· 失败 {tot_fail} · 传图 {tot_img} =====")
    log(f"（去重复用率 {round(100*tot_reuse/max(tot_q,1))}%；digests → {DIGDIR}）")
    prog.close()


if __name__ == "__main__":
    main()
