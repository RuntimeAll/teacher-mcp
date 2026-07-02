"""整卷批量录入：解析 omml_to_text 转出的卷子文本 → 经 MCP stdio client 真录入 biz_question + 组卷 biz_paper。

确定性拆题（题号、/【来源】/【答案】/【解析】/章节头）+ 逐题 login→[图自动传OSS]→format_question→ingest_question → create_paper。
🔴 图自动化：从原 docx 按 rId 抽图 → upload_image 传 OSS → 把 ossUrl 嵌进 stem 的 ![](url)（format 转图块）+ ingest_question.images（biz_question_image）。全代码完成，不手动传。
🔴 PRD-C-208 ⑤ 管线迁移：拆题/金标/去重逻辑上提 app/paperparse.py（单一事实源），本脚本只留「MCP 调用编排 + 图传 OSS」；
   sync_ingest/run_paper 从这里 re-export 的 parse_paper/plain_text 保持可用。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\ingest_paper.py --txt 卷.txt --docx 原卷.doc --subject-id 100 --batch ID --paper-name "卷名" --category-id 3001004004
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.paperparse import (  # noqa: E402,F401  单一事实源 = app/paperparse.py；F401: parse_paper/plain_text 供 sync_ingest/run_paper re-export
    FIG, FIG_IDS, dedup_key, derive_region, derive_source_type, derive_year,
    load_docx_media, parse_paper, plain_text)
from tools.dbutil import errlog as _errlog  # noqa: E402  MCP 噪声日志引文件，控制台干净省 token

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
IMGDIR = ROOT / ".paper_imgs"


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


async def run(args):
    text = Path(args.txt).read_text(encoding="utf-8")
    questions = parse_paper(text)
    if args.limit:
        questions = questions[: args.limit]
    figs = sum(1 for q in questions if q["has_fig"])
    print(f"[parse] {len(questions)} 题（含图 {figs} 题）")
    if args.dry:
        for q in questions[:5]:
            print(json.dumps(q, ensure_ascii=False)[:300])
        return

    # docx 抽图准备
    z = rid_map = None
    if args.docx and os.path.exists(args.docx):
        IMGDIR.mkdir(exist_ok=True)
        z, rid_map = load_docx_media(args.docx)
        print(f"[img] docx 媒体就绪，rId 映射 {len(rid_map)} 个")
    else:
        print("[img] 未给 --docx 或文件不存在 → 跳过传图（图占位）")

    paper_name = args.paper_name or (text.split("\n", 1)[0].strip() if text else args.batch)
    # 金标：年份 + 地点 + 类型（卷名派生，可 --exam-year/--region-code/--source-type 兜底），每题继承
    gb_year = args.exam_year or derive_year(paper_name)
    gb_region = args.region_code or derive_region(paper_name)
    gb_type = args.source_type or derive_source_type(paper_name)
    print(f"[金标] 年份={gb_year or '?'} 地点={gb_region or '?'} 类型={gb_type}（1中考/2模拟/3期末/4月考/5单元）")
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    ok, fail, reused, ids = 0, 0, 0, []
    img_cache = {}     # rId -> ossUrl
    uploaded = 0

    async with stdio_client(server, errlog=_errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok"), f"login 失败 {login}"
            print(f"[login] teacher_id={login.get('teacher_id')}")

            async def resolve_rid(rid):
                """rId → (ossUrl, assetId)（抽 media 写本地 → upload_image 传 OSS，缓存）。"""
                nonlocal uploaded
                if rid in img_cache:
                    return img_cache[rid]
                url = aid = None
                tgt = (rid_map or {}).get(rid) if rid_map else None
                if tgt and z is not None:
                    try:
                        data = z.read("word/" + tgt.replace("\\", "/"))
                        ext = os.path.splitext(tgt)[1] or ".png"
                        fp = IMGDIR / f"{args.batch}_{rid}{ext}"
                        fp.write_bytes(data)
                        up = _unwrap(await session.call_tool(
                            "upload_image", {"local_path": str(fp), "asset_kind": "figure"}))
                        if up.get("ok"):
                            url = up.get("oss_url")
                            aid = up.get("asset_id")
                            uploaded += 1
                    except Exception as e:
                        print(f"  ⚠ 图 {rid} 传失败 {type(e).__name__}")
                img_cache[rid] = (url, aid)
                return url, aid

            async def imagify(s, role, meta):
                """把 s 里的 〖图:rId..〗 替换成 ![](ossUrl)，并收集 images meta（含 assetId，否则底座跳过 biz_question_image）。"""
                for m in list(FIG_IDS.finditer(s)):
                    rids = [x.strip() for x in m.group(1).split(",") if x.strip().startswith("rId")]
                    repl = ""
                    for rid in rids:
                        u, aid = await resolve_rid(rid)
                        if u:
                            repl += f"![]({u})"
                            meta.append({"ossUrl": u, "assetId": aid, "role": role})
                    s = s.replace(m.group(0), repl, 1)
                s = FIG.sub("", s)  # 清掉无 rId 的 〖图〗
                return s

            for q in questions:
                images_meta = []
                stem_md = await imagify(q["stem"], "stem", images_meta)
                opts_md = [await imagify(o, "figure", images_meta) for o in q["options"]]
                analyze_md = await imagify(q["analyze"], "analysis", images_meta)

                block_json = ""
                fmt = _unwrap(await session.call_tool("format_question", {
                    "question_type": q["type"], "stem": stem_md, "options": opts_md}))
                if fmt.get("ok"):
                    block_json = fmt.get("block_json") or ""

                ing = _unwrap(await session.call_tool("ingest_question", {
                    "subject_id": args.subject_id,
                    "question_type": q["type"],
                    "difficult": 2,
                    "stem_text": plain_text(stem_md) or "（见原卷）",
                    "block_json": block_json,
                    "answer_text": q["answer"],
                    "analyze_text": analyze_md,
                    "images": images_meta,
                    # 🔴 去重键=题干纯字符：同题干→复用既有题、仅加本卷引用，不新建重复行
                    "external_key": dedup_key(plain_text(stem_md), args.batch, q["num"]),
                    "exam_year": gb_year,
                    "region_code": gb_region,
                    "source_type": gb_type,
                    "source_raw": q["source"] or paper_name,
                    "status": "1",
                }))
                if ing.get("ok"):
                    ok += 1
                    ids.append(ing.get("question_id"))
                    if ing.get("created") is False:
                        reused += 1
                        print(f"  ♻ 第{q['num']}题 题干已存在 → 复用 qid={ing.get('question_id')}，仅加本卷引用")
                else:
                    fail += 1
                    print(f"  ❌ 第{q['num']}题 {ing.get('reason')}")
    print(f"[done] batch={args.batch} ok={ok}(去重复用{reused}) fail={fail} total={len(questions)} 传图={uploaded}")

    if ids and not args.no_paper:
        async with stdio_client(server, errlog=_errlog()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("login", {})
                cp = _unwrap(await session.call_tool("create_paper", {
                    "name": paper_name, "question_ids": [int(x) for x in ids],
                    "paper_category_id": args.category_id or ""}))
                if cp.get("ok"):
                    pid = cp.get("paper_id")
                    # 🔴 page() 按 p.subject_id likeRight 筛目录，/create 不设 subject_id → 脚本补设=目录节点 id，卷才在目录下可见
                    if args.category_id and pid:
                        _set_paper_subject(pid, args.category_id)
                    # 分值(通值)+ 建议时长：走 update_paper 按年级标准分算
                    up = _unwrap(await session.call_tool("update_paper", {
                        "paper_id": int(pid), "total_score": args.total_score, "suggest_time": args.suggest_time}))
                    sc = f"总分{up.get('total')}" if up.get("ok") else f"分值失败:{up.get('reason')}"
                    print(f"[paper] ✅ 建卷 paper_id={pid} 「{paper_name}」 {cp.get('question_count')}题 cat={args.category_id} {sc} 时长{args.suggest_time}min")
                else:
                    print(f"[paper] ❌ {cp.get('reason')}")


def _set_paper_subject(paper_id, category_id):
    """建卷后补设 biz_paper.subject_id=目录节点 id（page 按它筛目录）。PRD-C-208 起统一走 app/db。"""
    try:
        from app.db import set_paper_subject
        set_paper_subject(paper_id, category_id)
    except Exception as e:
        print(f"  ⚠ subject_id 补设失败 {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", required=True)
    ap.add_argument("--docx", default="")
    ap.add_argument("--subject-id", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--paper-name", default="")
    ap.add_argument("--category-id", default="")
    ap.add_argument("--exam-year", default="")      # 金标·年份兜底
    ap.add_argument("--region-code", default="")    # 金标·地点兜底（国标行政区划）
    ap.add_argument("--source-type", type=int, default=0)  # 类型兜底
    ap.add_argument("--total-score", type=int, default=120)   # 试卷总分(通值，初中常规120)
    ap.add_argument("--suggest-time", type=int, default=120)  # 建议时长(分钟)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-paper", action="store_true")
    ap.add_argument("--dry", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
