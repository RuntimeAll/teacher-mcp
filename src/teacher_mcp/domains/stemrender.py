"""题干渲图（确定性·零 LLM）—— PRD-O-005 D8 方案 A「渲图旁路」。

把纯文本题干（可含 $LaTeX$、markdown 粗体/表格降级）渲成一张白底试卷版式 PNG，
供举一反三引擎（图驱动，opus 多模态 OCR）读图。引擎侧只要图干净清晰即可，
渲染无需像素级精确——LaTeX 源码 opus 也读得懂，故一切失败均降级为纯文本渲染、绝不抛。

技术选型：matplotlib（Agg，非 pyplot 全局态）。
  - 中文走 Microsoft YaHei / SimHei（C:\\Windows\\Fonts），rcParams fallback；
  - `$...$` 段走 mathtext；解析失败的行 **原样当纯文本渲**（parse_math=False），并置 degraded=True；
  - 任何未预期异常 → 最终兜底：全文纯文本渲染（parse_math 全关）。

对外唯一入口：render_stem(stem, options=None, out_path="") -> dict
  返回 {ok, path, width, height, degraded}。out_path 省略则落系统临时目录。
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # 无头渲染，绝不弹窗
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# ───────────────────────── 渲染常量（试卷版式）─────────────────────────
_DPI = 200
_FONT_PT = 13          # 正文字号（pt）；@200dpi ≈ 36px，试卷合适
_LINE_STEP = 1.75      # 行距倍率（相对字高）
_MARGIN_PX = 56        # 四周边距
_MAX_VISUAL = 32       # 每行最大视觉宽（CJK 计 1.0，ascii 计 ~0.55）
_MIN_VISUAL = 18       # 图宽下限（避免短题渲成窄条）
_TALL_STEP = 2.55      # 含分式/根号/大运算符的行加高行距（避免上下行叠字）
# 触发加高的高矮 LaTeX 构件（分式/根式/求和积分/上下标堆叠）
_TALL_MATH = re.compile(r"\\d?frac|\\sqrt|\\sum|\\int|\\prod|\\lim|\\binom|\^\{|_\{")

# 中文字体候选（存在即注册；家族名用于 rcParams）
_FONT_FILES = [
    (r"C:\Windows\Fonts\msyh.ttc", "Microsoft YaHei"),
    (r"C:\Windows\Fonts\simhei.ttf", "SimHei"),
]
_SANS_STACK = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
_font_ready = False


def _ensure_fonts() -> None:
    """把系统中文字体登记进 matplotlib（幂等），并配好 sans-serif fallback 栈。"""
    global _font_ready
    if _font_ready:
        return
    for path, _name in _FONT_FILES:
        try:
            if os.path.exists(path):
                font_manager.fontManager.addfont(path)
        except Exception:
            pass  # 字体登记失败不致命，退回 matplotlib 自带字体
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = _SANS_STACK
    matplotlib.rcParams["axes.unicode_minus"] = False  # 负号用 ascii，避免缺字
    _font_ready = True


# ───────────────────────── 文本预处理 ─────────────────────────
_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")   # ![alt](url) → [图]
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")           # **x** / __x__ → x
_ITALIC = re.compile(r"(?<![*_])(\*|_)(?!\s)(.+?)(?<!\s)\1(?![*_])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")      # markdown 标题标记
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _demarkdown(line: str) -> str:
    """markdown 轻降级为纯文本排版（图占位→[图]、去粗斜体标记、表格转空格分隔）。"""
    line = _IMG_MD.sub("[图]", line)
    line = _HEADING.sub("", line)
    if _TABLE_SEP.match(line):
        return ""  # 表格分隔行（|---|---|）直接吃掉
    if "|" in line and line.strip().startswith("|"):
        # markdown 表格行：竖线转三空格，读起来是分列纯文本
        line = line.strip().strip("|")
        line = re.sub(r"\s*\|\s*", "   ", line)
    line = _BOLD.sub(r"\2", line)
    line = _ITALIC.sub(r"\2", line)
    return line.rstrip()


def _visual_len(s: str) -> float:
    """视觉宽估算：CJK/全角 1.0，其余 ~0.55（math 段按 0.6/字符粗估）。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o >= 0x1100 and (
            0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or
            0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
            0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60 or
            0xFFE0 <= o <= 0xFFE6
        ):
            w += 1.0
        else:
            w += 0.55
    return w


# `$...$` 段（非贪婪，允许转义 \$ 不算边界）；奇数个 $ 时余下按纯文本
_MATH_SEG = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$")


def _tokenize(line: str) -> list[tuple[str, bool]]:
    """把一行切成 [(片段, is_math)]；math 片段（含两侧 $）原子不可断。"""
    tokens: list[tuple[str, bool]] = []
    pos = 0
    for m in _MATH_SEG.finditer(line):
        if m.start() > pos:
            tokens.append((line[pos:m.start()], False))
        tokens.append((m.group(0), True))  # 含 $...$
        pos = m.end()
    if pos < len(line):
        tokens.append((line[pos:], False))
    return tokens


def _wrap(line: str, max_visual: float) -> list[str]:
    """按视觉宽折行：math 段整体保留，纯文本段按字符切；不在 $...$ 内断行。"""
    if not line:
        return [""]
    out: list[str] = []
    cur = ""
    cur_w = 0.0

    def flush():
        nonlocal cur, cur_w
        out.append(cur)
        cur, cur_w = "", 0.0

    for seg, is_math in _tokenize(line):
        if is_math:
            w = _visual_len(seg) * 1.1
            if cur_w + w > max_visual and cur:
                flush()
            cur += seg
            cur_w += w
        else:
            for ch in seg:
                cw = _visual_len(ch)
                if cur_w + cw > max_visual and cur:
                    flush()
                cur += ch
                cur_w += cw
    flush()
    # 去掉纯空尾行但至少留一行
    while len(out) > 1 and out[-1] == "":
        out.pop()
    return out


def _prep_options(options: Optional[list]) -> list[str]:
    """选择题选项 → 每项一行，缺 A./B. 前缀则补（A B C D …）。"""
    if not options:
        return []
    letters = "ABCDEFGH"
    lines: list[str] = []
    for i, opt in enumerate(options):
        s = _demarkdown(str(opt or "").strip())
        if not re.match(r"^[A-H][.、．)\s]", s):
            prefix = letters[i] if i < len(letters) else str(i + 1)
            s = f"{prefix}. {s}"
        lines.append(s)
    return lines


def _build_lines(stem: str, options: Optional[list]) -> list[str]:
    """题干 + 选项 → 逻辑行（未折行）。"""
    raw = (stem or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logical: list[str] = []
    for ln in raw:
        logical.append(_demarkdown(ln))
    # 折叠连续空行为最多一空行
    collapsed: list[str] = []
    for ln in logical:
        if ln == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(ln)
    opt_lines = _prep_options(options)
    if opt_lines:
        if collapsed and collapsed[-1] != "":
            collapsed.append("")
        collapsed.extend(opt_lines)
    # 去首尾空行
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return collapsed or [""]


# ───────────────────────── 渲染 ─────────────────────────
def _default_out_path() -> str:
    fd, path = tempfile.mkstemp(prefix="stemrender_", suffix=".png")
    os.close(fd)
    return path


def _render_lines(display_lines: list[str], out_path: str, force_plain: bool) -> tuple[int, int, bool]:
    """把已折好的显示行渲成 PNG。返回 (width_px, height_px, degraded)。

    force_plain=True → 全行 parse_math=False（终极兜底路径）。
    否则逐行验证 math：解析失败的行退回纯文本并置 degraded。
    """
    _ensure_fonts()
    lines = display_lines or [""]
    # 图宽：取最宽行视觉宽（math 段粗估），夹在 [_MIN_VISUAL, _MAX_VISUAL]
    widest = max((_visual_len(re.sub(r"\$", "", ln)) for ln in lines), default=_MIN_VISUAL)
    widest = max(_MIN_VISUAL, min(_MAX_VISUAL, widest))
    font_px = _FONT_PT / 72.0 * _DPI
    base_step = font_px * _LINE_STEP
    tall_step = font_px * _TALL_STEP
    # 每行行距：含高矮 math 构件的行加高（分式上下不叠字）
    steps = [tall_step if (not force_plain and _TALL_MATH.search(ln)) else base_step for ln in lines]
    content_w = widest * font_px
    width_px = int(content_w + 2 * _MARGIN_PX)
    height_px = int(sum(steps) + 2 * _MARGIN_PX)

    fig = Figure(figsize=(width_px / _DPI, height_px / _DPI), dpi=_DPI)
    fig.patch.set_facecolor("white")
    canvas = FigureCanvasAgg(fig)
    renderer = canvas.get_renderer()

    x = _MARGIN_PX / width_px  # 左边距（figure 分数）
    y_cursor = height_px - _MARGIN_PX  # 从顶部起，逐行下移（像素）
    degraded = False

    for line, step in zip(lines, steps):
        y = y_cursor / height_px
        y_cursor -= step
        parse_math = (not force_plain) and ("$" in line)
        t = fig.text(x, y, line if line else " ", fontsize=_FONT_PT,
                     ha="left", va="top", parse_math=parse_math, color="black")
        if parse_math:
            try:
                t.get_window_extent(renderer)  # 触发 mathtext 解析；坏 LaTeX 抛 ValueError
            except Exception:
                # 逃生仓：该行原样当纯文本渲
                t.set_parse_math(False)
                degraded = True

    canvas.draw()
    fig.savefig(out_path, dpi=_DPI, facecolor="white")
    return width_px, height_px, degraded


def render_stem(stem: str, options: Optional[list] = None, out_path: str = "") -> dict:
    """题干（markdown+$LaTeX$）→ 白底试卷版式 PNG。确定性、零 LLM、永不抛。

    参数:
      stem    : 题干文本，可含 $LaTeX$、markdown 粗体/表格（表格降级为空格分隔纯文本）、
                ![](url) 图占位（渲为「[图]」——无图题本无图，仅防御）。
      options : 选择题选项列表（可含或不含 A./B. 前缀，工具自动补），每项独立成行。
      out_path: 落盘路径；省略则落系统临时目录（tempfile）。
    返回:
      {ok, path, width, height, degraded}
        - degraded=True ⇒ 至少一行 LaTeX 解析失败已退回纯文本渲（图仍可用，opus 读源码无碍）。
        - 任何未预期异常 ⇒ 兜底为全文纯文本渲染（degraded=True）；仍返回可用 PNG。
    """
    path = (out_path or "").strip() or _default_out_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = _default_out_path()

    try:
        display_lines: list[str] = []
        for ln in _build_lines(stem, options):
            display_lines.extend(_wrap(ln, _MAX_VISUAL))
        w, h, degraded = _render_lines(display_lines, path, force_plain=False)
        return {"ok": True, "path": path, "width": w, "height": h, "degraded": degraded}
    except Exception:
        # 终极逃生仓：全文纯文本渲染（parse_math 全关）——绝不外抛
        try:
            display_lines = []
            for ln in _build_lines(stem, options):
                display_lines.extend(_wrap(ln, _MAX_VISUAL))
            w, h, _ = _render_lines(display_lines or [str(stem or "")[:200]], path, force_plain=True)
            return {"ok": True, "path": path, "width": w, "height": h, "degraded": True}
        except Exception as e:  # noqa: BLE001
            # 连兜底都塌（几乎不可能）——返回不 ok，但仍不抛，交调用方走原软拒绝语义
            return {"ok": False, "path": "", "width": 0, "height": 0,
                    "degraded": True, "error": f"{type(e).__name__}: {e}"}
