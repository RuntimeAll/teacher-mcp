"""G7（AC1）：stdio 真起 gate —— 旧 .mcp.json 形态（python -m + env ROLE）真子进程握手。

防「in-memory 绿、stdio 崩」（编码/入口/env 读取只有真子进程能暴露）。
三个旧角色 env 值各起一次：握手 + list_tools 数量与视图断言一致。
"""
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# 旧 .mcp.json 形态：command=python -m teacher_mcp.server + env TEACHER_MCP_ROLE
# O-005 溯源增强：新增共享工具 my_recent_uploads，各角色视图 +1
# MCP 收口（2026-07-13）基线：prep=32（+update/archive_teach_target 学科归位轮 +special3 收口）；
# 新增 shelf 角色=14（shared6+health+my_recent_uploads+shelf6）；all=56
# 2026-07-15：+bind_book_node_to_lesson（tags={"prep"}，课次绑书章节材料位）→ all 55→56、prep 32→33
# 2026-07-19：+计算题出题器 list_calc_types/generate_calc_paper（tags={"prep"}）→ all +2、prep +2
# 2026-07-20（PRD-007）：+login_as（tags={"shared"}，免密切身份）→ 所有角色视图各 +1
# 补记漂移（2026-07-30 随 PRD-013 批1 对账，此前几卡上线未同步计数）：
#   +feedback5（PRD-009，prep）+shuzimi1（data+prep）+delete_questions1（data）
#   +generate_calc_items1（prep）→ all 59→70、prep 36→43、data 23→25
# 2026-07-30（PRD-013 批1）：+每日打卡四工具（tags={"prep"}）→ all 70→71、prep 43→47
EXPECT = {"all": 71, "prep": 47, "data": 25, "ingest": 18, "lecture": 17, "variant": 16, "shelf": 15}


@pytest.mark.asyncio
@pytest.mark.parametrize("role,n", sorted(EXPECT.items()))
async def test_stdio_handshake_per_role(role: str, n: int):
    t = StdioTransport(
        command=PY,
        args=["-m", "teacher_mcp.server"],
        env={"TEACHER_MCP_ROLE": role, "PYTHONIOENCODING": "utf-8"},
        cwd=str(ROOT),
    )
    async with Client(t, timeout=30) as c:
        tools = await c.list_tools()
        names = sorted(t_.name for t_ in tools)
        assert len(names) == n, f"ROLE={role} 工具数 {len(names)} != {n}: {names}"
        assert "login" in names and "health_check" in names
