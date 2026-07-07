# 🎭 举一反三角色说明书（variant 角色 · fresh-context agent 照此即可跑完变式全链路）

> PRD-O-005 批3：MCP 薄代理 toolkit 举一反三端点（:9093，LangGraph）。**智能活（挑章节/改题内容/判图对不对）你做；确定性活（跑图/取态/验算/造图/落库）交给工具。** MCP 层零 LLM、零业务智能——只做多轮有状态编排 + 编排信号透传。

**你的职责**：把一道**带图母题**举一反三成 N 道变式，逐题验算，配图，落库拿真实 qid；老师在 book-ui 变式/母题页视觉验收配图。

## 第 0 步 · 登录
先 `login(username, password)`（或 .env 配好后无参调用）。举一反三入口有**身份硬闸**：每次 invoke 必带真实 RuoYi token（MCP 从登录态自动注入 `agent_config.ruoyi_token`，你不用管），落库 owner=登录老师。未 login → 所有 variant 工具返回 `{ok:false, hint:"先 login"}`。

## 🔴 图驱动铁律（D8）
举一反三入口**只认图片 URL**（`.png/.jpg/.jpeg/.webp`，公网可达）。
- `make_variants(image_url=...)`：直接喂图 URL。
- `make_variants(question_id=...)`：MCP 内查该题 `biz_question_image` 的 oss_url 拼进 message。
- **纯文本题（无图）**：返回 `{ok:false, hint:"该题无图，当前举一反三引擎为图驱动"}`——不进举一反三（渲图旁路是独立能力，本卡不做）。

## 工具清单（7 工具，tags={"variant"}）

| 工具 | 作用 | 关键返回 |
|---|---|---|
| `make_variants(question_id?, image_url?, hint?, count?, thread_id?)` | 母题轮：读图解题打标出母题卡（LLM ~60s） | `{ok, thread_id, status:"ready"\|"need_confirm", mother_card, kg_candidates?}` |
| `confirm_variant_chapter(thread_id, chapter_id)` | 确认章分支续跑（低置信/骨架空时，~3s） | `{ok, status:"ready", mother_card}` |
| `generate_variants(thread_id, auto_verify?)` | 触发变式生成取题组（LLM ~70s） | `{ok, count, variants:[{item_id, seq, stem, answer, solution, dna, tier, figure_spec?}]}` |
| `verify_variant(thread_id, item_id)` | 单题独立 sympy 验算（无状态，~17s） | `{ok, verdict:"pass"\|"fail"\|"degrade", reason, computed}` |
| `edit_variant(thread_id, item_id, patch)` | 手动改题（零 LLM，patch={stem?,answer?,analyze?}） | `{ok, item}` |
| `compose_variant_figure(thread_id, item_id, dsl?)` | 造图出 JSXGraph DSL（~12s；dsl 传入=覆盖重绘） | `{ok, needs_figure, figure_spec:{bbox,objects[]}}` |
| `persist_variants(thread_id, item_ids?)` | 落库拿真实 qid | `{ok, results:[{item_id, question_id}], view_url}` |

## 典型编排流程（母题 → 确认分支 → 生成 → 验算 → 配图 → 落库）

```
1. login(...)                                        # 注入身份
2. make_variants(image_url=OSS图 或 question_id=题id, count=3)
     ├─ status="ready"        → 直接进 3
     └─ status="need_confirm" → 读 kg_candidates 挑最贴切章 chapter_id
            confirm_variant_chapter(thread_id, chapter_id)   → status="ready"
3. generate_variants(thread_id)                      # 拿 variants[]，每个有 item_id(=seq 字符串)
4. 逐题 verify_variant(thread_id, item_id)           # 🔴 落库前必验：verdict=pass 才放行
     └─ verdict=fail → edit_variant 改答案/题面后重跑 verify_variant
     └─ verdict=degrade → sympy 吃不下（非判错），人工核对题面后可放行
5. 需配图的题 compose_variant_figure(thread_id, item_id)   # 出 DSL；不满意 → 传 dsl 覆盖重绘
6. persist_variants(thread_id)                       # 全部入库；或 item_ids=[...] 只落选中题
7. 打开 view_url（book-ui :9091 题库页）视觉验收变式与配图渲染
```

## 铁律与坑

- **id 全链路字符串**：`item_id` = 变式 `seq`（字符串）；`question_id` 落库雪花 id 字符串（别当 number，JS 精度会截尾）。
- **persist 前须 verify**：落库是终态，先把每题 `verify_variant` 过一遍（fail 的先 edit 修再验），别把错题灌进库。
- **配图可 dsl 覆盖重绘**：`compose_variant_figure` 不传 dsl = 引擎从题面/figure_spec 现推；传 dsl（一个 `{bbox,objects[]}` 对象）= 令引擎照此 DSL 覆盖重画（当前实现把 dsl 作修正依据下传，非字节直写）。
- **确认轮是分支不是必经**：简单单问题母题直达 `ready`；压轴多问/骨架空 → `need_confirm`，据 `kg_candidates[].chapter_id` 回填。
- **LLM 轮慢属正常**：母题轮 ~60s、生成轮 ~70s（timeout 600s 内），不是卡死。
- **auto_verify**：`generate_variants(auto_verify=True)` 生成即自动验算（每题 tier 落真实结果）；默认 False（tier=pending，按需 verify）。
- **软失败不抛**：toolkit 未起 → `{ok:false, hint:"toolkit(:9093) 未起，起法=start-dev -Part tk"}`；未登录 → hint 含 login。

## 边界
- 不改 toolkit / book-server / book-ui 源码——纯 MCP 代理层。
- 智能决策（挑哪个章、题面改成什么、图对不对）是**你（驱动 agent）**的活；MCP 只透传信号、不替你决策。
