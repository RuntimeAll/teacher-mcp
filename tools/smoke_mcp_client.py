"""A1 预飞行：用真实 MCP stdio client 连 teacher-mcp server，跑 login → list_kg_tree → compose_paper。

证明 MCP 协议层(工具发现+调用) + RuoYi 双头鉴权透传 + 真落库 biz_paper 全链路成立，
且不需重启本 Claude session（这里我们自己当 MCP client harness）。

用法（cwd=teacher-mcp）:
  # 阶段A：列工具 + 登录 + 拉树（dump 到 .smoke_tree.json）
  .venv\\Scripts\\python.exe tools\\smoke_mcp_client.py
  # 阶段B：带 subjectId 组卷（id 取自树/经 mysql 确认有题）
  .venv\\Scripts\\python.exe tools\\smoke_mcp_client.py <subjectId> [<subjectId> ...]
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def _unwrap(result):
    """从 CallToolResult 取 dict（优先 structuredContent，回退解析 text）。"""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP 把返回 dict 包成 {"result": {...}} 或直接结构化
        return sc.get("result", sc)
    for c in getattr(result, "content", []) or []:
        txt = getattr(c, "text", None)
        if txt:
            try:
                return json.loads(txt)
            except Exception:
                return {"_text": txt}
    return {}


def _collect_leaves(nodes, path=None):
    path = path or []
    out = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        nm = str(n.get("name", "")).strip()
        children = n.get("children") or []
        cur = path + [nm]
        if not children:
            out.append({"id": str(n.get("id", "")), "name": nm, "path": " / ".join(cur)})
        else:
            out.extend(_collect_leaves(children, cur))
    return out


async def main(subject_ids):
    env = dict(os.environ)
    server = StdioServerParameters(
        command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=env
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) 列工具
            tools_resp = await session.list_tools()
            names = [t.name for t in tools_resp.tools]
            print(f"[tools] {names}")
            assert "login" in names, "缺 login 工具"
            assert "compose_paper" in names, "缺 compose_paper 工具（写工具，非只读 health）"
            assert "list_kg_tree" in names, "缺 list_kg_tree 工具"

            # 2) login（无参 → 用 .env 兜底真账号）
            login_res = _unwrap(await session.call_tool("login", {}))
            print(f"[login] {login_res}")
            assert login_res.get("ok"), f"login 失败: {login_res}"
            teacher_id = login_res.get("teacher_id")
            print(f"[login] teacher_id={teacher_id}")

            # 3) list_kg_tree
            tree_res = _unwrap(await session.call_tool("list_kg_tree", {}))
            assert tree_res.get("ok"), f"list_kg_tree 失败: {tree_res}"
            nodes = tree_res.get("nodes") or []
            leaves = _collect_leaves(nodes)
            (ROOT / ".smoke_tree.json").write_text(
                json.dumps({"leaves": leaves}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[tree] 顶层 {len(nodes)} 节点, 叶子 {len(leaves)} 个 → .smoke_tree.json")
            for lf in leaves[:8]:
                print(f"       leaf id={lf['id']} | {lf['path']}")

            # 4) compose_paper（仅当传了 subjectId）
            if subject_ids:
                outline = [
                    {"subjectId": sid, "questionType": 1, "difficult": 2, "count": 3}
                    for sid in subject_ids
                ]
                comp = _unwrap(
                    await session.call_tool(
                        "compose_paper", {"outline": outline, "title": "A1预飞行组卷"}
                    )
                )
                print(f"[compose] {json.dumps(comp, ensure_ascii=False)[:600]}")
                if comp.get("ok"):
                    print(f"[compose] ✅ paper_id={comp.get('paper_id')} item_count={comp.get('item_count')}")
                else:
                    print(f"[compose] ❌ {comp.get('reason')}")
            else:
                print("[compose] 跳过（未传 subjectId）。下一步：选一个有题的叶子 id 再跑阶段B。")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
