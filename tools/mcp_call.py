"""通用 MCP 工具调用器（stdio harness）：不重启 Claude session 就能调 teacher-mcp 任意工具。

gates 回归 / fresh-context 验收 / 手工单发都用它——每次调用自起 stdio server 子进程，
默认先 login（.env 凭据），再调目标工具，结果 JSON 打到 stdout。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\mcp_call.py <tool> '<json-args>'      # 内联 JSON
  .venv\\Scripts\\python.exe tools\\mcp_call.py <tool> --file args.json  # 大参数走文件（整卷 items 推荐）
  .venv\\Scripts\\python.exe tools\\mcp_call.py --list                   # 列全部工具
例:
  tools\\mcp_call.py resolve_kg '{"subject_root":"100","query":"乘方","leaves_only":true}'
  tools\\mcp_call.py ingest_items --file batch.json
"""
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
from tools.dbutil import errlog  # noqa: E402  MCP 噪声日志引文件，控制台干净

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
NO_LOGIN = {"login", "convert_doc", "convert_pdf", "parse_paper_text", "resolve_kg"}  # 无需会话的工具


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


async def run(tool, args):
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool == "--list":
                tools = await session.list_tools()
                for t in tools.tools:
                    print(f"- {t.name}")
                return
            if tool not in NO_LOGIN:
                login = _unwrap(await session.call_tool("login", {}))
                if not login.get("ok"):
                    print(json.dumps({"ok": False, "reason": f"login 失败: {login}"}, ensure_ascii=False))
                    return
            res = _unwrap(await session.call_tool(tool, args))
            print(json.dumps(res, ensure_ascii=False, indent=1))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    tool = sys.argv[1]
    args = {}
    if len(sys.argv) >= 3:
        if sys.argv[2] == "--file":
            args = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        else:
            args = json.loads(sys.argv[2])
    asyncio.run(run(tool, args))


if __name__ == "__main__":
    main()
