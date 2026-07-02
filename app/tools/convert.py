"""MCP 工具·转换层（PRD-C-208 ②确定性辅助）：convert_doc / convert_pdf / parse_paper_text。

🔴 三个全是**确定性预处理**（零 LLM、零落库、不需 login）——智能（拆题重组/多模态读图/锚定判断）
   归驱动 agent。路由见 README「录入角色说明书」路由矩阵。
🔴 H1a spike（2026-07-03）：文字层 PDF 公式抽取不可用（上标丢/根指数错位/分数断裂）→
   convert_pdf 永远按页转图；文字层全文只作辅助（题号定位/纯文字题），拆题以页图多模态为准。
"""
import json
import zipfile
from pathlib import Path

from app import docconv, paperparse

ROOT = Path(__file__).resolve().parent.parent.parent
WORK = ROOT / ".paper_work"
IMGDIR = ROOT / ".paper_imgs"


def register(mcp, client=None) -> None:
    @mcp.tool()
    def convert_doc(doc_path: str, batch: str = "") -> dict:
        """Word（.docx / docx伪装的.doc）→ 结构化文本 + 题图清单。确定性 XML 解析（OMML→$LaTeX$），非 OCR。

        产出文本里：数学公式=$LaTeX$；图=〖图:rId〗占位（与 images 的 rid 对应）。
        下一步：文本喂 parse_paper_text 确定性拆题；images[].local_path 喂 upload_image 传 OSS。
        参数: doc_path 卷子绝对路径；batch 批次名（图文件名前缀+文本落盘名，空=文件名主干）。
        返回: {ok, text, paras, text_path, images:[{rid, local_path}]}；真 OLE .doc → {ok:false, reason:"另存为 docx"}。
        """
        p = Path(doc_path)
        if not p.exists():
            return {"ok": False, "reason": f"文件不存在: {doc_path}"}
        name = batch or p.stem.replace("#", "_").replace(" ", "_")
        try:
            text, paras = docconv.docx_to_text(str(p))
        except zipfile.BadZipFile:
            return {"ok": False, "reason": "真 OLE .doc（非 docx 伪装），请用 Word 另存为 .docx 再录"}
        except KeyError as e:
            return {"ok": False, "reason": f"docx 结构缺 {e}（不是标准 Word 文档？）"}
        WORK.mkdir(exist_ok=True)
        txt_path = WORK / f"{name}.txt"
        txt_path.write_text(text, encoding="utf-8")
        imgs = docconv.extract_docx_images(str(p), IMGDIR, name)
        return {
            "ok": True, "text": text, "paras": paras, "text_path": str(txt_path),
            "images": [{"rid": rid, "local_path": lp} for rid, lp in sorted(imgs.items())],
        }

    @mcp.tool()
    def convert_pdf(pdf_path: str, batch: str = "", dpi: int = 170, max_pages: int = 0) -> dict:
        """PDF → 文字层检测 + 按页转图（多模态拆题的原料）。确定性 pymupdf，非 OCR。

        🔴 拆题一律以页图多模态直读为准（H1a：文字层公式不可信）；text_layer 仅辅助（题号定位/纯文字题）。
        参数: pdf_path 绝对路径；batch 页图文件名前缀（空=文件名主干）；dpi 渲染精度（170 实测够）；max_pages 限页（0=全部）。
        返回: {ok, page_count, has_text_layer, pages:[页图路径...], text_layer_path?}。
        """
        p = Path(pdf_path)
        if not p.exists():
            return {"ok": False, "reason": f"文件不存在: {pdf_path}"}
        name = batch or p.stem.replace("#", "_").replace(" ", "_")
        try:
            has_text, n, pages, text = docconv.pdf_probe_and_render(str(p), IMGDIR, name, dpi=dpi, max_pages=max_pages)
        except Exception as e:
            return {"ok": False, "reason": f"PDF 解析失败: {type(e).__name__}: {e}"}
        out = {"ok": True, "page_count": n, "has_text_layer": has_text, "pages": pages}
        if has_text and text.strip():
            WORK.mkdir(exist_ok=True)
            tp = WORK / f"{name}_textlayer.txt"
            tp.write_text(text, encoding="utf-8")
            out["text_layer_path"] = str(tp)
            out["note"] = "文字层仅辅助：公式不可信（H1a），拆题用 pages 页图多模态"
        return out

    @mcp.tool()
    def parse_paper_text(text: str = "", text_path: str = "") -> dict:
        """规整卷面文本 → 题列表 JSON（确定性规则拆题，零 LLM）。适合 convert_doc 产物 / 规整粘贴文本。

        拆题规则：题号「N、/N．/N.」+ 章节头判题型 + 【来源】/【答案】/【解析】+ 多小问状态机 + 卷尾参考答案回填。
        产出每题含 〖图:rId〗 占位（如有）。🔴 agent 拿到后须核对题数/补漏，再构造 IngestItem[] 喂 ingest_items；
        位置序号（数组下标+1）= 入库 sort，别用 num（原卷题号遇杂散「N.」会错位——七上教训）。
        参数: text 卷面文本（与 text_path 二选一，text 优先）。
        返回: {ok, count, questions:[{num,type,stem,options,answer,analyze,has_fig,source,score}], digest}。
        """
        if not text and text_path:
            fp = Path(text_path)
            if not fp.exists():
                return {"ok": False, "reason": f"text_path 不存在: {text_path}"}
            text = fp.read_text(encoding="utf-8")
        if not text.strip():
            return {"ok": False, "reason": "text 与 text_path 至少给一个（且非空）"}
        qs = paperparse.parse_paper(text)
        return {"ok": True, "count": len(qs), "questions": qs,
                "digest": paperparse.make_digest(qs, stem_width=90)}
