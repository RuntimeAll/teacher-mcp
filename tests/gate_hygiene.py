"""G8（AC7）：git 卫生闸 —— 新仓不进二进制题图/暂存产物（旧仓 48M 题图入库的教训）。

断言：git ls-files 里
  ① 无图片扩展名文件（tests/fixtures/ 白名单除外）；
  ② 无 >200KB 文件（白名单显式列举，不做模式兜底放行）。
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SIZE_WHITELIST: set[str] = set()  # 超 200KB 的合法文件在此显式列举（当前应为空）


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def test_no_binary_images_tracked():
    bad = [f for f in _tracked()
           if Path(f).suffix.lower() in IMG_EXT and not f.startswith("tests/fixtures/")]
    assert not bad, f"图片类文件入库: {bad}"


def test_no_large_files_tracked():
    bad = []
    for f in _tracked():
        if f in SIZE_WHITELIST:
            continue
        p = ROOT / f
        if p.exists() and p.stat().st_size > 200 * 1024:
            bad.append((f, p.stat().st_size))
    assert not bad, f">200KB 文件入库(不在白名单): {bad}"
