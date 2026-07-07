"""题干渲图旁路单测（纯本地、无网络、无 LLM）—— PRD-O-005 D8 方案 A。

覆盖三条：
  ① 中文 + $LaTeX$ 混排 → 出 PNG（文件存在、尺寸>0、not degraded）。
  ② 故意喂非法 LaTeX → degraded=true，仍出图不抛。
  ③ 选择题带 options → ok（选项分行渲染）。
逃生仓语义（永不抛）由 ② 守护：坏 LaTeX 不得让 render_stem 抛。
"""
import os

from teacher_mcp.domains.stemrender import render_stem


def _png_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(8)


def test_render_chinese_latex_mixed(tmp_path):
    """中文 + LaTeX 混排：出图、尺寸>0、not degraded。"""
    out = str(tmp_path / "mix.png")
    r = render_stem("已知 $x^2-5x+6=0$，求 x 的值。**提示**：因式分解 $(x-2)(x-3)=0$。", out_path=out)
    assert r["ok"] is True
    assert r["path"] == out and os.path.exists(out)
    assert r["width"] > 0 and r["height"] > 0
    assert r["degraded"] is False
    assert _png_bytes(out).startswith(b"\x89PNG")


def test_render_bad_latex_degrades_no_raise(tmp_path):
    """非法 LaTeX（不闭合/坏命令）→ degraded=true，仍出可用 PNG，绝不抛。"""
    out = str(tmp_path / "bad.png")
    r = render_stem(r"坏 $\frac{1}{$ 不闭合，另 $\undefinedcmd{x}$ 也坏", out_path=out)
    assert r["ok"] is True
    assert os.path.exists(out) and r["width"] > 0 and r["height"] > 0
    assert r["degraded"] is True


def test_render_choice_with_options(tmp_path):
    """选择题带 options：ok，出图（选项自动补 A./B. 分行）。"""
    out = str(tmp_path / "choice.png")
    r = render_stem("下列哪个数是质数？", options=["4", "6", "7", "9"], out_path=out)
    assert r["ok"] is True
    assert os.path.exists(out) and r["width"] > 0 and r["height"] > 0
    # 4 个选项各占一行 → 高度应明显大于纯单行题干
    r_single = render_stem("下列哪个数是质数？", out_path=str(tmp_path / "single.png"))
    assert r["height"] > r_single["height"]


def test_render_default_tempfile():
    """省略 out_path → 落系统临时目录，返回可用路径。"""
    r = render_stem("简单题干 $a+b$")
    try:
        assert r["ok"] is True and os.path.exists(r["path"])
        assert r["path"].endswith(".png")
    finally:
        try:
            os.remove(r["path"])
        except OSError:
            pass
