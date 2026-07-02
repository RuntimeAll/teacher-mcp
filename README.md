# teacher-mcp · RuoYi 平台能力 MCP 适配层（PRD-C-1000 探针）

> 落位 = `D:\workplace\book-ai\teacher-mcp`（顶层「本地 Claude Code 编排层」，跨 codeplace 线，不埋在 codeplace-C 内）。
> 本地 Claude Code 当**编排层**，经本 MCP server 把 `RuoYi:8090` 现有 HTTP 接口当工具调，
> 以**真实 teacher 账号身份**完成备课动作（探针能力=组卷，真落库 `biz_paper`）。
> 🔴 两底座（RuoYi:8090 / toolkit:8093）**源码零改**——本 server 是独立新增薄代理层，随时可拆。

## 范式（PRD 核心洞察）
```
[本地 Claude Code]  ← LLM/编排：听意图、查树后选知识点、决定 outline（"算"在这）
      │ stdio（MCP 协议：工具发现 + 调用）
[teacher-mcp]       ← 薄适配层：登录拿双头 token 注入身份 + 确定性代理 RuoYi HTTP（不算 LLM、不私存业务数据）
      │ HTTP（Authorization: Bearer + clientid，envelope 解包）
[RuoYi :8090]       ← 业务事实库：查知识树 / 确定性组卷 / 落库 biz_paper / 权限审计
```
组卷的"算"（意图解析、从树选知识点叶子）由 Claude Code 自己做；MCP 工具只暴露**确定性平台能力**。

## 工具集（Claude↔平台契约）
**身份/读：**
| 工具 | 作用 | 落点 |
|---|---|---|
| `login(username, password)` | 真账号登录拿双头 token，注入会话身份（token 不回吐明文） | `/auth/login` + `/system/user/getInfo` 取 userId |
| `list_kg_tree()` | 查知识点树（组卷/录入选叶子用） | `/teacher/question/lazyTree` |

**能力①组卷（探针）：**
| 工具 | 作用 | 落点 |
|---|---|---|
| `compose_paper(outline, title?)` | 按大纲确定性组卷 + **真落库** `biz_paper`，归属登录 teacher | `/teacher/paper/auto-generate`（save=true + teacherId=会话 userId） |

`outline` = `[{subjectId, subjectName?, questionType(1选/4填/5简), difficult(1-4), count}]`，`subjectId` 取自 `list_kg_tree` 叶子 id。

**能力②录入（`tools/ingest.py`，AC6 第二能力活证）：**
| 工具 | 作用 | 落点 |
|---|---|---|
| `format_question(question_type, stem, options?)` | markdown 题干+选项 → 确定性 blockJson（三端渲染，底座永不抛） | `/teacher/format/to-block` |
| `upload_image(local_path, asset_kind?)` | 本地图直传 OSS + 去重，返回 ossUrl | `/teacher/ingest/image` |
| `ingest_question(subject_id, question_type, difficult, stem_text, block_json?, answer_text?, analyze_text?, knowledge_ids?, images?, external_key?, status?)` | 事务录一道题 + KG 关联，**真落库** `biz_question`，归属登录 teacher | `/teacher/ingest/question`（teacher_id 由 LoginHelper 注入） |

录入题型码：`1选择/2填空/3判断/4计算/5解答/6作图`；难度：`1基础/2提升/3压轴`。
🔴 录入范式：**Claude Code 读试卷(多模态)→ 拆题 + 抽题干/选项/答案/解析 + 选知识点叶子**（"算"在 Claude）；
每题 `format_question`(→blockJson) → `ingest_question`(→落库)；带图先 `upload_image`。`external_key` 去重幂等。
冒烟：`tools\smoke_ingest.py`。

## 🔴 加能力 = 加一个目录（AC6 解耦）
身份层(`tools/auth.py`) / 读层(`tools/kg.py`) / 写层(`tools/compose.py`)已分离。
二期 `make_variants` / `ingest_textbook` / `build_kg` 照样：
1. 新增 `app/tools/<name>.py`，写 `def register(mcp, client): @mcp.tool() async def <name>(...)`；
2. `app/server.py` 加一行 `tool_<name>.register(mcp, _client)`（占位接线注释已留）。
`login` / 鉴权 / 底座调用层（`app/ruoyi.py`）**不动**。

## 跑 / 自检
前置：RuoYi C 线 :8090 在跑（`curl --noproxy "*" http://localhost:8090/actuator/health` 返 401=活）。
```powershell
# 装依赖（独立 venv，别和 toolkit 混）
uv venv; uv pip install --python .venv\Scripts\python.exe mcp httpx pydantic pydantic-settings anyio
copy .env.example .env   # 填 RUOYI_USERNAME/RUOYI_PASSWORD（不入 git）

# A1 冒烟（自带 MCP stdio client harness，不需重启 Claude session）
.venv\Scripts\python.exe tools\smoke_mcp_client.py                       # 阶段A：列工具+登录+拉树
.venv\Scripts\python.exe tools\smoke_mcp_client.py 100001002002 100001001001001  # 阶段B：组卷落库
```

## 真·Claude Code 终验（G1）
1. 把 `mcp.json.example` 的 `teacher-mcp` 块合进 `book-ai/.mcp.json`；
2. **重启 Claude session**（🔴 MCP 不热加载）；
3. 对 Claude Code 说「以 teacher001 登录，给『有理数与数轴』这章出套卷」→ 它调 login→list_kg_tree→compose_paper 落库。

## 边界
- 不改底座源码（纯 HTTP 代理）；不上线（本期只本地跑通）；不上 LangChain/LangGraph（薄适配层）。
- 探针口径：单进程单会话（stdio 单 client）。多会话 token_ref 句柄隔离是后续卡的事。
