# -*- coding: utf-8 -*-
"""就地修复图片横排：把讲解片段里「同一 docx 段落的多张图」被拆成的连续 block image 节点，
合并成「一个 paragraph 内多个 inlineImage」→ Umo 按 group=inline 横向流成一排。
- 增量，不重录：直接读现有 content_json（已是 ossUrl）改布局，kgExample/文字/顺序全保留。
- 行(哪些图算一排)以 docx 为准：faithful 解析每段 drawing rid，≥2 张的段=一排；再按 rid→ossUrl 映到 DB 节点。
用法: python pipeline/fix_image_rows.py <docx> <course_id> [book_id=CC7S]
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from tools.dbutil import errlog  # noqa: E402
from app import lectureconv, db  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
DOCX = sys.argv[1]
COURSE = sys.argv[2]
BOOK = sys.argv[3] if len(sys.argv) > 3 else "CC7S"


def _unwrap(result):
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    for c in getattr(result, "content", []) or []:
        if getattr(c, "text", None):
            try:
                return json.loads(c.text)
            except Exception:
                return {"_text": c.text}
    return {}


def _rid_rows(docx):
    """docx 每段 drawing rid；一段 ≥2 张 = 一排。返回 [[rid,...], ...]（有序）。"""
    doc = Document(docx)
    rows = []
    for p in doc.paragraphs:
        rids = []
        for dr in p._p.findall(".//" + qn("w:drawing")):
            blip = dr.find(".//" + qn("a:blip"))
            rid = blip.get(qn("r:embed")) if blip is not None else None
            if rid:
                rids.append(rid)
        if len(rids) >= 2:
            rows.append(rids)
    return rows


def _inline_img(node):
    """block image 节点 → inlineImage 节点（同 src/width/height + inline:true）。"""
    a = dict(node.get("attrs", {}))
    a["inline"] = True
    return {"type": "inlineImage", "attrs": a}


def _merge_rows(content, url_rows):
    """把 content 里连续、src 命中同一 url_row 的 image 节点合并成 paragraph(inlineImage)。"""
    changed = 0
    out = []
    i = 0
    n = len(content)
    while i < n:
        node = content[i]
        if node.get("type") == "image":
            src = node.get("attrs", {}).get("src")
            row = next((r for r in url_rows if src in r), None)
            if row:
                # 贪心吃掉后续同 row 的连续 image 节点
                grp = [node]
                j = i + 1
                while j < n and content[j].get("type") == "image" and content[j].get("attrs", {}).get("src") in row:
                    grp.append(content[j])
                    j += 1
                if len(grp) >= 2:
                    out.append({"type": "paragraph", "content": [_inline_img(g) for g in grp]})
                    changed += 1
                    i = j
                    continue
        out.append(node)
        i += 1
    return out, changed


async def main():
    rid_rows = _rid_rows(DOCX)
    print("[docx] 多图段(排):", [len(r) for r in rid_rows], rid_rows)
    # rid → ossUrl：重跑 extract+upload（幂等，url_hash 去重返回同 url）
    batch = Path(DOCX).stem.replace("#", "_").replace(" ", "_")
    _, images_dict, _ = lectureconv.faithful_content(DOCX)
    imgs = lectureconv.extract_images(DOCX, str(ROOT / ".lecture_imgs"), batch, images_dict)
    rid2path = {i["rid"]: i["local_path"] for i in imgs}

    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok") and login.get("teacher_id") == 1, f"需 admin: {login}"
            print("[login]", login.get("username"))
            # rid → ossUrl（幂等上传）
            rid2url = {}
            need = {r for row in rid_rows for r in row}
            for rid in sorted(need, key=lambda r: int(re.sub(r"\D", "", r) or 0)):
                p = rid2path.get(rid)
                if not p:
                    print(f"  [img] {rid} 无本地文件，跳过")
                    continue
                r = _unwrap(await session.call_tool("upload_image",
                            {"local_path": str(Path(p).resolve()), "asset_kind": f"kg_lecture_{COURSE}"}))
                if r.get("oss_url"):
                    rid2url[rid] = r["oss_url"]
            url_rows = [[rid2url[r] for r in row if r in rid2url] for row in rid_rows]
            url_rows = [r for r in url_rows if len(r) >= 2]
            print("[url_rows]", [len(r) for r in url_rows])

            # 读现有片段 → 合并 → 存回
            cn = db.conn()
            with cn.cursor() as c:
                c.execute("SELECT subject_id, title, content_json FROM biz_kg_lecture_frag "
                          "WHERE subject_id LIKE %s AND book_id=%s AND owner_id=1 AND CHAR_LENGTH(subject_id)=15 "
                          "ORDER BY subject_id", (COURSE + "%", BOOK))
                rows = c.fetchall()
            cn.close()
            frags = []
            for sid, title, cj in rows:
                content = json.loads(cj)["content"]
                new_content, changed = _merge_rows(content, url_rows)
                if changed:
                    frags.append({"subjectId": sid, "title": title,
                                  "contentJson": {"type": "doc", "content": new_content}})
                    print(f"  [{sid}] «{title}» 合并 {changed} 排图")
            if not frags:
                print("无可合并片段（本课时讲解区无多图段）")
                return
            save = _unwrap(await session.call_tool("save_lecture_frag",
                        {"frags": frags, "book_id": BOOK}))
            print("[save]", json.dumps(save, ensure_ascii=False)[:300])


asyncio.run(main())
