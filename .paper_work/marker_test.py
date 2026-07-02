# 验证 ingest_items 原生 〖图:rId〗 标记自动替换（AC7 反馈②根治的回归）：
# convert_doc → parse_paper_text → 直接把 questions 的题面(含〖图:rId〗)喂 ingest_items(images带rid)，不手动替换
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
# 一份带图的同步练习卷（数轴，图多）
DOC = "D:/workplace/book-ai/预研空间/试卷下载/初中数学/同步练习/七上/1.2 数轴#1.doc"


async def main():
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            cv = _unwrap(await session.call_tool("convert_doc", {"doc_path": DOC, "batch": "MARKER"}))
            assert cv.get("ok"), cv
            rid2path = {im["rid"]: im["local_path"] for im in cv["images"]}
            print(f"[convert] paras={cv['paras']} 图={len(rid2path)}")
            pp = _unwrap(await session.call_tool("parse_paper_text", {"text": cv["text"]}))
            assert pp.get("ok"), pp
            # 只取前 3 道带图题验证 marker 替换
            picked = [q for q in pp["questions"] if q["has_fig"]][:3]
            print(f"[parse] {pp['count']} 题，取 {len(picked)} 道带图题验证")
            items = []
            for q in picked:
                # 收集该题题面/解析里的 rid，构造 images[].rid（不手动替换 marker！交给工具）
                rids = set()
                for field in (q["stem"], q["analyze"], *q["options"]):
                    for m in FIG_IDS.finditer(field):
                        for r in m.group(1).split(","):
                            r = r.strip()
                            if r.startswith("rId") and r in rid2path:
                                rids.add(r)
                items.append({
                    "stem": q["stem"], "options": q["options"], "answer": q["answer"],
                    "analyze": q["analyze"], "question_type": q["type"], "score": q["score"],
                    "images": [{"local_path": rid2path[r], "rid": r, "role": "stem"} for r in sorted(rids)],
                    "source_raw": "PRD-C-208 AC7根治回归 marker自动替换", "source_type": 6,
                    "external_key": f"PRDC208-MARKER-{q['num']}",
                })
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok"), login
            res = _unwrap(await session.call_tool("ingest_items", {"items": items, "subject_root": "100"}))
            print(json.dumps({"stats": res.get("stats"), "results": res.get("results")}, ensure_ascii=False))
            # 断言：入库题干里无残留 〖图 标记、有 ![](oss) 图
            qids = [r["question_id"] for r in res["results"] if "question_id" in r]
            import pymysql
            c = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                                database="ai_lesson_prep", charset="utf8mb4")
            cur = c.cursor()
            fmt = ",".join(["%s"] * len(qids))
            cur.execute(f"SELECT id, stem_text FROM biz_question WHERE id IN ({fmt})", qids)
            ok = True
            for qid, stem in cur.fetchall():
                residue = "〖图" in (stem or "")
                cur.execute("SELECT COUNT(*) FROM biz_question_image WHERE question_id=%s", (qid,))
                imgn = cur.fetchone()[0]
                print(f"  q{qid}: 残留〖图标记={residue} 图行={imgn}")
                if residue or imgn == 0:
                    ok = False
            c.close()
            print()
            print("MARKER PASS: 〖图:rId〗 标记自动替换、无残留、图入库" if ok else "MARKER FAIL")


asyncio.run(main())
