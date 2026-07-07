"""PRD-O-005 H1 预飞行：FastMCP(装到 3.4.3) tag 角色过滤 + in-memory Client 是否如预期。

验证三件事：
  1. @mcp.tool(tags={...}) 打标 + server 级 include_tags 过滤 → 角色视图
  2. in-memory Client 能 list_tools / call_tool（G 系测试的地基）
  3. 过滤是注册期还是暴露期（决定我们「一份工具定义、按 ROLE 多视图」怎么写）
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from fastmcp import FastMCP, Client


def build(include_tags=None):
    mcp = FastMCP("spike")

    @mcp.tool(tags={"shared"})
    def login(username: str) -> dict:
        return {"ok": True, "user": username}

    @mcp.tool(tags={"data"})
    def ingest_items(items: list) -> dict:
        return {"ok": True, "n": len(items)}

    @mcp.tool(tags={"variant"})
    def make_variants(question_id: str) -> dict:
        return {"ok": True, "qid": question_id}

    if include_tags is not None:
        mcp.enable(tags=include_tags, only=True)  # v3 口径：暴露期启停
    return mcp


async def main():
    # 1) 全量视图
    async with Client(build()) as c:
        names_all = sorted(t.name for t in await c.list_tools())
    print("all:", names_all)
    assert names_all == ["ingest_items", "login", "make_variants"], names_all

    # 2) 角色视图 = include_tags 过滤
    async with Client(build(include_tags={"shared", "variant"})) as c:
        names_variant = sorted(t.name for t in await c.list_tools())
        # 3) in-memory 调用
        r = await c.call_tool("make_variants", {"question_id": "123"})
        print("variant view:", names_variant, "| call:", r.data)
    assert names_variant == ["login", "make_variants"], names_variant
    assert r.data == {"ok": True, "qid": "123"}

    # 4) 被过滤工具不可调（暴露期过滤的硬证据）
    async with Client(build(include_tags={"shared"})) as c:
        try:
            await c.call_tool("ingest_items", {"items": []})
            print("FAIL: filtered tool callable")
            return 1
        except Exception as e:
            print("filtered tool blocked:", type(e).__name__)

    print("H1 PASS: tag filter + in-memory client OK on fastmcp", __import__("fastmcp").__version__)
    return 0


sys.exit(asyncio.run(main()))
