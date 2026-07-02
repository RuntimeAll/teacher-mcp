"""录入能力冒烟：MCP stdio client 连 server，跑 login → format_question → ingest_question 真落库。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\smoke_ingest.py
"""
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def _unwrap(result):
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    for c in getattr(result, "content", []) or []:
        txt = getattr(c, "text", None)
        if txt:
            try:
                return json.loads(txt)
            except Exception:
                return {"_text": txt}
    return {}


async def main():
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            login = _unwrap(await session.call_tool("login", {}))
            print(f"[login] {login}")
            assert login.get("ok")

            fmt = _unwrap(await session.call_tool("format_question", {
                "question_type": 1,
                "stem": "下列各数中，最小的有理数是（  ）",
                "options": ["$-3$", "$-1$", "$0$", "$2$"],
            }))
            print(f"[format] degraded={fmt.get('degraded')} block_json_len={len(fmt.get('block_json') or '')}")
            assert fmt.get("ok") and fmt.get("block_json")

            ing = _unwrap(await session.call_tool("ingest_question", {
                "subject_id": "100",
                "question_type": 1,
                "difficult": 1,
                "stem_text": "下列各数中，最小的有理数是（  ）",
                "block_json": fmt["block_json"],
                "answer_text": "A",
                "analyze_text": "负数小于 0 小于正数，且 $-3<-1$，故最小的是 $-3$。选 A。",
                "knowledge_ids": [{"kpId": "100001002002", "isPrimary": 1, "source": "U"}],
                "external_key": "MCP-INGEST-SMOKE-001",
                "exam_year": "2026",
                "source_raw": "teacher-mcp 录入冒烟",
                "status": "1",
            }))
            print(f"[ingest] {ing}")
            if ing.get("ok"):
                print(f"[ingest] ✅ question_id={ing.get('question_id')} created={ing.get('created')}")
            else:
                print(f"[ingest] ❌ {ing.get('reason')}")


if __name__ == "__main__":
    asyncio.run(main())
