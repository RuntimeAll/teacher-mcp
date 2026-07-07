# teacher-mcp（重建版）· 老师系统操作全权代理 MCP

> **定位**：老师在系统里能做的操作，agent 经本 MCP 都能替他做；老师只负责线上视觉验收（每个写工具返回 `view_url` 验收入口）。
> **血统**：PRD-O-005（正本=`codeplace-O/prd/PRD-O-005/`），以旧 `ai-bkb/teacher-mcp`（探针版，PRD-C-1000→C-208→C-213）为参考重建。
> **端口/拓扑权威** = `ai-bkb/workspaces.json`（本文不复述端口表）。

## 架构

```
[CLI agent (Claude Code 等)]   ← 智能/编排：读题拆题、选知识点、挑章节、判难度（"算"在这）
      │ stdio（MCP）
[teacher-mcp]                  ← 确定性代理：登录注入身份 + 双底座 HTTP + 转换/查表；零 LLM
      │                          ├─ backends/ruoyi.py   → book-server :9090（envelope+clientid，401 自动重登一次）
      │                          ├─ backends/toolkit.py → toolkit :9093（FastAPI，举一反三引擎，ruoyi_token 注入）
      │                          └─ backends/db.py      → MySQL :3307（pymysql 收口，挂账：对外开放前 HTTP 化）
[平台]                          ← 业务事实库 + book-ui 展示（老师视觉验收）
```

- **三线三角色（tag 视图）**：`data`（录题+录卷+录讲义）/ `prep`（备课）/ `variant`（举一反三）；旧 ROLE 值 `ingest`/`lecture` 保留为兼容别名。`all`（缺省）=全量。
- **加能力 = 加文件**：新工具函数进对应 `tools/*.py` 打 tag；新角色 = `server.py` ROLE_TAGS 加一行；新底座 = `backends/` 加一个 client。
- **角色说明书** = `src/teacher_mcp/manuals/{data,prep,variant}.md`（`get_role_manual` 工具/resource 读文件，fresh agent 先调它）。

## 跑 / 自检

```powershell
# 依赖（独立 venv）
uv venv; uv pip install --python .venv\Scripts\python.exe -e . --group dev
copy .env.example .env   # 填 RUOYI_USERNAME/PASSWORD（不入 git）

# 测试（gate 全套；G3/G4/G6 基线/G7 需 BE:9090 在跑，G4 另需 toolkit:9093）
.venv\Scripts\python.exe -m pytest tests -q
# 变式链冒烟（不落库，到 compose-figure）
.venv\Scripts\python.exe scripts\smoke_variant.py
```

**.mcp.json 条目**（同一入口按 ROLE 分视图；MCP 不热加载，改后重启 session）：

```jsonc
{
  "mcpServers": {
    "prep-assistant":    { "command": "<本仓>\\.venv\\Scripts\\python.exe", "args": ["-m", "teacher_mcp.server"],
      "env": { "PYTHONPATH": "<本仓>\\src", "TEACHER_MCP_ROLE": "prep",    "RUOYI_USERNAME": "…", "RUOYI_PASSWORD": "…" } },
    "qbank-assistant":   { "同上，TEACHER_MCP_ROLE": "data（或旧值 ingest）" },
    "lecture-assistant": { "同上，TEACHER_MCP_ROLE": "data（或旧值 lecture）" },
    "variant-assistant": { "同上，TEACHER_MCP_ROLE": "variant" }
  }
}
```

## 工具面（42 = 旧 34 全兼容 + health_check + 7 变式）

| 组 | 工具 |
|---|---|
| shared（全角色） | login / list_kg_tree / resolve_kg / search_questions / get_question / get_role_manual / **health_check**(新) |
| data·录题 | convert_doc / convert_pdf / parse_paper_text / format_question / upload_image / ingest_question / **ingest_items**(统一入库口) / verify_ingest / label_question |
| data·讲义 | convert_lecture_docx / save_lecture_frag / remove_lecture_frag / list_lecture_docs / get_lecture_content |
| prep | schedule 11 工具 + compose_paper / create_paper / update_paper |
| **variant（新）** | make_variants / confirm_variant_chapter / generate_variants / verify_variant / edit_variant / compose_variant_figure / persist_variants —— 编排流程见 `manuals/variant.md` |

## 纪律与已知边界

- 🔴 代码空间只放代码：题图/暂存/dump 一律不入库（.gitignore 建仓即闸，G8 gate 断言）。
- 🔴 D8：举一反三入口只认**带图题**（引擎为图驱动）；纯文本题返回 ok:false + hint。
- 🔴 /teacher/** 在 BE 端完全无鉴权（2026-07-07 实测，不登录也可调）——本仓不修（权限收窄另立卡），MCP 仍走真实登录（落库归属/审计）。
- pymysql 旁路（backends/db.py）挂账：对外开放（stdio→HTTP）前须 HTTP 化。
- 转换器（domains/）与旧仓字节级等价（G5 gate 守护），改它先想想 \xa0。
