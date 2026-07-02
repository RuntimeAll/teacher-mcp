# PRD-C-208 G3 驱动（驱动 agent 的 Word 路线编排，可复现）：
# convert_doc → parse_paper_text → 构造 IngestItem[]（〖图:rId〗→![](local_path)+images） → ingest_items(+paper)
# 用法: python .paper_work/docx_route.py <doc> <subject_root> <category_id> <paper_name> [--dry]
import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from tools.dbutil import errlog  # noqa: E402
from tools.mcp_call import _unwrap  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
FIG_IDS = re.compile(r"〖图:([^〗]*)〗")
FIG = re.compile(r"〖图(?::[^〗]*)?〗")


def build_items(questions, img_map, source_type):
    """parse 产物 → IngestItem[]：〖图:rId〗 → ![](local_path) 占位 + images[].local_path（ingest_items 代传替换）。"""
    items = []
    for q in questions:
        imgs, seen = [], set()

        def sub(s, role):
            def rep(m):
                out = ""
                for rid in [x.strip() for x in m.group(1).split(",") if x.strip().startswith("rId")]:
                    lp = img_map.get(rid)
                    if lp:
                        out += f"![]({Path(lp).as_posix()})"
                        if (rid, role) not in seen:
                            seen.add((rid, role))
                            imgs.append({"local_path": lp, "role": role})
                return out
            return FIG.sub("", FIG_IDS.sub(rep, s))

        stem = sub(q["stem"], "stem")
        opts = [sub(o, "figure") for o in q["options"]]
        analyze = sub(q["analyze"], "analysis")
        items.append({
            "stem": stem, "options": opts, "answer": q["answer"], "analyze": analyze,
            "question_type": q["type"], "score": q["score"], "images": imgs,
            "source_raw": q["source"], "source_type": source_type,
        })
    return items


async def main():
    doc, root_id, cat, name = sys.argv[1:5]
    dry = "--dry" in sys.argv
    batch = re.sub(r"[^\w]", "", Path(doc).stem)[-12:]
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            cv = _unwrap(await session.call_tool("convert_doc", {"doc_path": doc, "batch": batch}))
            assert cv.get("ok"), f"convert_doc 失败: {cv}"
            print(f"[convert] paras={cv['paras']} 图={len(cv['images'])}")
            pp = _unwrap(await session.call_tool("parse_paper_text", {"text": cv["text"]}))
            assert pp.get("ok"), f"parse 失败: {pp}"
            print(f"[parse] {pp['count']} 题\n{pp['digest']}")
            if dry:
                return
            img_map = {im["rid"]: im["local_path"] for im in cv["images"]}
            items = build_items(pp["questions"], img_map, source_type=5)
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok"), f"login 失败: {login}"
            res = _unwrap(await session.call_tool("ingest_items", {
                "items": items, "subject_root": root_id,
                "paper": {"name": name, "category_id": cat, "total_score": 100, "suggest_time": 90},
            }))
            print(json.dumps({k: res.get(k) for k in ("ok", "stats", "paper_id", "note")}, ensure_ascii=False))
            for r in res.get("results", []):
                if "reason" in r or r.get("warnings"):
                    print(" ", r)


asyncio.run(main())
