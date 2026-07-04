# -*- coding: utf-8 -*-
"""讲解入库 runner（通用）：讲解 IR → 上传引用图 → save_lecture_frag 官方库 upsert。单 MCP 会话。
用法: python pipeline/save_lecture.py <讲解IR.json> <docx路径>
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
from app import lectureconv  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
IR_PATH = sys.argv[1]
DOCX = sys.argv[2]


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


def _collect_rids(frags):
    rids = set()

    def w(n):
        if isinstance(n, dict):
            if n.get("type") in ("image", "inlineImage"):
                r = n.get("attrs", {}).get("rid")
                if r:
                    rids.add(r)
            for v in n.values():
                w(v)
        elif isinstance(n, list):
            for v in n:
                w(v)
    for f in frags:
        w(f["contentJson"])
    return rids


async def main():
    ir = json.load(open(IR_PATH, encoding="utf-8"))
    batch = Path(DOCX).stem.replace("#", "_").replace(" ", "_")
    course = ir["course_subject_id"]
    _, images_dict, _ = lectureconv.faithful_content(DOCX)
    imgs = lectureconv.extract_images(DOCX, str(ROOT / ".lecture_imgs"), batch, images_dict)
    rid2path = {i["rid"]: i["local_path"] for i in imgs}
    rids = sorted(_collect_rids(ir["frags"]), key=lambda r: int(re.sub(r"\D", "", r) or 0))

    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok") and login.get("teacher_id") == 1, f"需 admin/uid1: {login}"
            print("[login]", login)
            image_map = {}
            for i, rid in enumerate(rids, 1):
                p = rid2path.get(rid)
                if not p:
                    print(f"  [img {i}/{len(rids)}] {rid} 无本地文件(EMF等)，跳过")
                    continue
                r = _unwrap(await session.call_tool("upload_image",
                            {"local_path": str(Path(p).resolve()), "asset_kind": f"kg_lecture_{course}"}))
                url = r.get("oss_url")
                if url:
                    image_map[rid] = url
                print(f"  [img {i}/{len(rids)}] {rid} -> {'...' + url[-22:] if url else 'FAIL ' + str(r)[:60]}")
            print(f"[upload] {len(image_map)}/{len(rids)}")
            save = _unwrap(await session.call_tool("save_lecture_frag",
                        {"frags": ir["frags"], "book_id": ir["book_id"], "image_map": image_map}))
            print("[save]", json.dumps(save, ensure_ascii=False)[:500])


asyncio.run(main())
