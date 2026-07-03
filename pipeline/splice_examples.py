# -*- coding: utf-8 -*-
"""例题入库+splice runner：例题提取产物 → 入题库拿 qid → 从讲解片段删例题节点段、原位插 kgExample → 重存。
单 MCP 会话：login → ingest_items(例题) → (pymysql 读现讲解) → 拼 kgExample → save_lecture_frag。
用法: python pipeline/splice_examples.py <examples.json> <docx路径> [book_id=CC7S] [subject_root=901]
"""
import asyncio
import json
import os
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

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
EXAMPLES_PATH = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / ".lecture_work" / "1.2.1_examples.json")
DOCX = sys.argv[2] if len(sys.argv) > 2 else r"D:\workplace\book-ai\预研空间\2025秋七上科学崔崔老师讲义\1.2.1科学测量——长度的测量  体积的测量（教师版）.docx"
BOOK_ID = sys.argv[3] if len(sys.argv) > 3 else "CC7S"
SUBJECT_ROOT = sys.argv[4] if len(sys.argv) > 4 else "901"


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


async def main():
    ex = json.load(open(EXAMPLES_PATH, encoding="utf-8"))
    examples = ex["examples"]
    course_prefix = examples[0]["kp_subjectId"][:12]  # 课时 L4 前缀
    batch_label = Path(DOCX).stem.replace("#", "_").replace(" ", "_")
    # rid → 本地图路径
    _, images_dict, _ = lectureconv.faithful_content(DOCX)
    imgs = lectureconv.extract_images(DOCX, str(ROOT / ".lecture_imgs"), batch_label, images_dict)
    rid2path = {i["rid"]: i["local_path"] for i in imgs}

    # 组例题 ingest 批（顺序 = examples 顺序，回来按序取 qid）
    batch = {"subject_root": SUBJECT_ROOT, "items": []}
    for e in examples:
        it = {"stem": e["stem"], "options": e.get("options", []), "answer": e.get("answer", ""),
              "analyze": e.get("analyze", ""), "question_type": e.get("question_type"), "kp_id": e["kp_subjectId"]}
        if e.get("source_raw"):
            it["source_raw"] = e["source_raw"]
        im = [{"local_path": rid2path[r], "rid": r, "role": "stem"} for r in e.get("image_rids", []) if rid2path.get(r)]
        if im:
            it["images"] = im
        batch["items"].append(it)

    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("teacher_id") == 1, f"需 admin: {login}"
            print("[login]", login)

            ing = _unwrap(await session.call_tool("ingest_items", batch))
            results = ing.get("results", [])
            print("[ingest 例题]", ing.get("stats"))
            qids = [r.get("question_id") for r in results]
            assert len(qids) == len(examples), f"qid数({len(qids)})≠例题数({len(examples)})"
            for e, q in zip(examples, qids):
                e["qid"] = q

            # 按知识点分组例题 span
            by_kp = {}
            for e in examples:
                by_kp.setdefault(e["kp_subjectId"], []).append(e)
                # 例题跨片段散落：extra_ranges = 其他片段里的孤儿答案/详解节点段——只删不插 kgExample
                for ex in e.get("extra_ranges", []) or []:
                    by_kp.setdefault(ex["kp_subjectId"], []).append(
                        {"kp_subjectId": ex["kp_subjectId"], "node_start": ex["node_start"],
                         "node_end": ex["node_end"], "delete_only": True})

            # 读当前讲解片段 content（DB，已是 ossUrl 图），splice
            cn = db.conn()
            spliced = []
            with cn.cursor() as c:
                c.execute("SELECT subject_id, title, content_json FROM biz_kg_lecture_frag "
                          "WHERE subject_id LIKE %s AND book_id=%s AND owner_id=1 "
                          "AND CHAR_LENGTH(subject_id)=15 ORDER BY subject_id",
                          (course_prefix + "%", BOOK_ID))
                rows = c.fetchall()
            cn.close()
            for sid, title, cj in rows:
                nodes = json.loads(cj)["content"]
                spans = sorted(by_kp.get(sid, []), key=lambda e: e["node_start"])
                if not spans:
                    continue  # 纯讲解片段不动
                new_nodes = []
                i = 0
                si = 0
                while i < len(nodes):
                    if si < len(spans) and i == spans[si]["node_start"]:
                        sp = spans[si]
                        # 起始节点混了讲解前缀（keep_prefix_runs）→ 保留前 N 个 text run 当讲解段
                        kpr = sp.get("keep_prefix_runs")
                        if kpr and nodes[i].get("type") == "paragraph":
                            kept_runs = nodes[i].get("content", [])[:kpr]
                            if kept_runs:
                                new_nodes.append({"type": "paragraph", "content": kept_runs})
                        if not sp.get("delete_only"):
                            new_nodes.append({"type": "kgExample",
                                              "attrs": {"qid": str(sp["qid"]), "knowledgeId": sid}})
                        i = sp["node_end"] + 1
                        si += 1
                    else:
                        new_nodes.append(nodes[i])
                        i += 1
                n_kge = sum(1 for s in spans if not s.get("delete_only"))
                n_del = sum(1 for s in spans if s.get("delete_only"))
                spliced.append({"subjectId": sid, "title": title,
                                "contentJson": {"type": "doc", "content": new_nodes}})
                print(f"  [{sid}] «{title}» 原{len(nodes)}节点 → {len(new_nodes)}节点, 插 {n_kge} kgExample, 删孤儿段 {n_del}")

            save = _unwrap(await session.call_tool("save_lecture_frag",
                                                   {"frags": spliced, "book_id": BOOK_ID}))
            print("[save 讲解]", json.dumps(save, ensure_ascii=False)[:400])


asyncio.run(main())
