"""MCP 工具·数字谜竖式图生成器（vertical-arithmetic figure）。

一工具（确定性程序绘图，纯 PIL，不走 LLM、不依赖浏览器）：
  render_shuzimi_figure  按结构化 puzzles 画竖式谜 PNG（□方框＝待填格），可选直传 OSS

立项背景（2026-07-27 拍板）：blockJson 块词汇表没有「竖式」语义块，纯文本录入的竖式谜
在三端渲染成一行字（七月小测格式债）。系统约定＝复杂版式走图片块；此前画图工艺是
HTML 表格→Edge 无头截图→PIL 裁边（服务器上没有 Edge，不可复现），本工具改为纯 PIL
确定性绘制，沉为 teacher-mcp 标准能力。

🔴 结构规范（本工具唯一契约）：
  puzzles: 1~4 个竖式，横向并排。每个竖式：
    {"label": "(1)",              # 可选角标，印在竖式左侧
     "rows": [                     # 从上到下逐行
        {"op": "",   "cells": "6?37"},
        {"op": "＋", "cells": "3??"},
        {"op": "HR"},              # 横线行（cells 忽略）
        {"op": "",   "cells": "7183"}
     ]}
  规则：cells 一个字符＝一格（数字/汉字/字母均可）；'?'＝待填□方框；
        各行按最长行右对齐自动补位；op＝行左侧运算符（＋ － × ÷，可空）；
        op="HR" ＝横线行，横线画满该竖式数字区全宽。

用途：数字谜/竖式计算题的题图 → 塞 blockJson image 块（biz_question_block）或
      ingest_question.images。答案版（格子里填好数字）把 '?' 换成实际字符再调一次即可。
字体：env SHUZIMI_FONT 指 TTF/TTC 绝对路径；缺省依次探测 Windows simhei/simsun 与
      Linux 常见 CJK 字体，找不到 CJK 字体时汉字会画成豆腐块（数字不受影响）。
"""
import os
import tempfile
import time

from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

# ── 绘制常量（2x 尺寸保证清晰，落图约等于卷面 34px 格）──
CELL = 68        # 格子边长 px
OP_W = 52        # 运算符列宽
HR_H = 16        # 横线行高
LABEL_W = 56     # 角标列宽
PAD = 24         # 画布内边距
GAP = 64         # 竖式之间横向间距
BOX_LINE = 4     # □方框线宽
HR_LINE = 5      # 横线线宽
FONT_SIZE = 44

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


class VRow(BaseModel):
    """竖式的一行：op=左侧运算符（＋ － × ÷ 或空；"HR"=横线行），cells=逐格字符（'?'=□待填）。"""

    op: str = Field(default="", description="行左侧运算符（＋ － × ÷，可空）；op=\"HR\" 表示横线行（cells 忽略）")
    cells: str = Field(default="", description="逐格字符串，一个字符一格；'?'＝待填□方框；数字/汉字/字母均可")


class Puzzle(BaseModel):
    """一个竖式。label 角标可选；rows 从上到下（数字行与 HR 横线行）。"""

    label: str = Field(default="", description="角标（如 \"(1)\"），印在竖式左侧；可空")
    rows: list[VRow] = Field(description="从上到下逐行；见模块头结构规范")


def _load_font():
    from PIL import ImageFont

    path = os.environ.get("SHUZIMI_FONT", "")
    candidates = ([path] if path else []) + _FONT_CANDIDATES
    for p in candidates:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, FONT_SIZE)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_puzzles(puzzles: list[Puzzle]):
    """纯函数：puzzles → PIL.Image（白底 RGB）。供工具与单测复用。"""
    from PIL import Image, ImageDraw

    font = _load_font()

    def puzzle_geom(p: Puzzle):
        width_cells = max((len(r.cells) for r in p.rows if r.op != "HR"), default=1)
        w = (LABEL_W if p.label else 0) + OP_W + width_cells * CELL
        h = sum(HR_H if r.op == "HR" else CELL for r in p.rows)
        return width_cells, w, h

    geoms = [puzzle_geom(p) for p in puzzles]
    total_w = PAD * 2 + sum(g[1] for g in geoms) + GAP * (len(puzzles) - 1)
    total_h = PAD * 2 + max((g[2] for g in geoms), default=CELL)

    im = Image.new("RGB", (total_w, total_h), (255, 255, 255))
    d = ImageDraw.Draw(im)

    x0 = PAD
    for p, (width_cells, w, h) in zip(puzzles, geoms):
        x_label = x0
        x_num = x0 + (LABEL_W if p.label else 0) + OP_W
        if p.label:
            d.text((x_label, PAD + CELL // 2 - FONT_SIZE // 2), p.label, fill=(0, 0, 0), font=font)
        y = PAD
        for r in p.rows:
            if r.op == "HR":
                d.line(
                    [(x_num - OP_W // 2, y + HR_H // 2), (x_num + width_cells * CELL, y + HR_H // 2)],
                    fill=(0, 0, 0), width=HR_LINE,
                )
                y += HR_H
                continue
            if r.op:
                d.text((x_num - OP_W, y + (CELL - FONT_SIZE) // 2), r.op, fill=(0, 0, 0), font=font)
            pad_cells = width_cells - len(r.cells)
            for i, ch in enumerate(r.cells):
                cx = x_num + (pad_cells + i) * CELL
                if ch == "?":
                    inset = 6
                    d.rectangle(
                        [cx + inset, y + inset, cx + CELL - inset, y + CELL - inset],
                        outline=(0, 0, 0), width=BOX_LINE,
                    )
                else:
                    bbox = d.textbbox((0, 0), ch, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    d.text((cx + (CELL - tw) // 2 - bbox[0], y + (CELL - th) // 2 - bbox[1]), ch, fill=(0, 0, 0), font=font)
            y += CELL
        x0 += w + GAP
    return im


def register(mcp, client: RuoyiClient) -> None:

    @mcp.tool(tags={"data", "prep"})
    async def render_shuzimi_figure(puzzles: list[Puzzle], upload: bool = True, out_name: str = "") -> dict:
        """数字谜竖式图生成器：按结构化 puzzles 确定性画竖式 PNG（'?'＝待填□方框），可直传 OSS。

        🔴 结构规范：puzzles=1~4 个竖式横向并排，每个 {label?, rows:[{op,cells}...]}；
        cells 一字符一格（数字/汉字均可），'?'＝□；op＝＋ － × ÷ 或空；op="HR"＝横线行。
        例（6□37＋3□□＝7183）：rows=[{"op":"","cells":"6?37"},{"op":"＋","cells":"3??"},
        {"op":"HR"},{"op":"","cells":"7183"}]。
        参数: upload=True 则直传 OSS（需先 login），False 只落本地；out_name 自定义文件名（可空）。
        返回: {ok, local_path, width, height, oss_url?, asset_id?}。
        产物用法: oss_url 塞 blockJson 图块（biz_question_block）或 ingest_question.images；
        答案版把 '?' 换成实际字符再调一次。
        """
        if not puzzles or len(puzzles) > 4:
            return {"ok": False, "reason": "puzzles 须为 1~4 个竖式"}
        for p in puzzles:
            if not any(r.op != "HR" and r.cells for r in p.rows):
                return {"ok": False, "reason": "每个竖式至少要有一行非 HR 的 cells"}
        try:
            im = draw_puzzles(puzzles)
        except Exception as e:  # 绘图失败不抛，返回原因
            return {"ok": False, "reason": f"绘图失败: {e}"}

        out_dir = os.path.join(tempfile.gettempdir(), "teacher-mcp-shuzimi")
        os.makedirs(out_dir, exist_ok=True)
        name = out_name.strip() or f"shuzimi-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        if not name.lower().endswith(".png"):
            name += ".png"
        local_path = os.path.join(out_dir, name)
        im.save(local_path)
        result = {"ok": True, "local_path": local_path, "width": im.width, "height": im.height}

        if upload:
            if not client.has_session():
                result.update({"oss_url": None, "asset_id": None, "upload_skipped": "需先 login（图已落本地）"})
                return result
            try:
                resp = await client.teacher_post(
                    "/teacher/ingest/image", {"localPath": local_path, "assetKind": "figure"}
                ) or {}
                result.update({"oss_url": resp.get("ossUrl"), "asset_id": resp.get("assetId"), "dedup": resp.get("dedup")})
            except RuoyiError as e:
                result.update({"oss_url": None, "asset_id": None, "upload_skipped": str(e)})
        return result
