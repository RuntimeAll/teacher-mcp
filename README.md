# teacher-mcp · RuoYi 平台能力 MCP 适配层

> 落位 = `D:\workplace\book-ai\teacher-mcp`（顶层「本地 Claude Code 编排层」，跨 codeplace 线，不埋在 codeplace-C 内）。
> 本地 Claude Code 当**编排层**，经本 MCP server 把平台 HTTP 接口当工具调，以**真实 teacher 账号身份**完成备课动作。
> 🔴 底座源码零改——本 server 是独立新增薄代理层，随时可拆。**MCP server 零 LLM、不私存业务数据（关系表 pymysql 收口见下 3.3-b）；智能全在驱动 agent。**
> 🔴 PRD 血统：PRD-C-1000（MCP 探针）→ **PRD-C-208（录入角色·全来源兼容封装）**。

## 范式（核心洞察）
```
[本地 Claude Code]  ← LLM/编排：听意图、多模态读题拆题、选知识点、判难度打标（"算"在这）
      │ stdio（MCP 协议：工具发现 + 调用）
[teacher-mcp]       ← 薄适配层：登录注入身份 + 确定性代理平台 HTTP + 确定性预处理（转换/拆题/查表）
      │ HTTP（Authorization Bearer + clientid，envelope 解包）；关系表少量 pymysql（3.3-b）
[平台 :8080]        ← 业务事实库：录题/组卷/落库/权限审计
```

---

# 🎭 录入角色说明书（PRD-C-208 · fresh-context agent 照此即可完成录入）

**你的职责**：不管题从哪来、长什么样，把它正确录进平台题库（入库+锚定+打标+可选成卷）。**智能活（拆题/读图/选知识点/判难度）你自己做；确定性活（转换/格式化/落库/去重）交给工具。**

## 第 0 步 · 登录
先 `login(username, password)`（或 .env 配好 RUOYI_USERNAME/PASSWORD 后无参调用）。所有写工具都需要会话身份；录的题 `create_by` = 登录 teacher。

## 路由矩阵（来源 → 处理路径）

| 来源 | 识别 | 路径 |
|---|---|---|
| **单题文本** | 你判断 | 直接构造 1 条 IngestItem → `ingest_items` |
| **多题文本/粘贴** | 你判断 | `parse_paper_text`（确定性拆）或你自拆 → 补全 → `ingest_items` |
| **Word（.docx / docx伪装.doc）** | 后缀+PK头 | `convert_doc` → `parse_paper_text` → 你补全 → `ingest_items`。**图直接把题面里的 `〖图:rId4〗` 标记原样留着，并在 `images` 里给 `{local_path: convert_doc返回的该rid路径, rid:"rId4"}`——工具会自动把标记替换成图并清残留**（无需你手动改题面） |
| **PDF（文字层）** | pymupdf 检出文字层 | 🔴 数学/科学卷公式抽取不可信（H1a spike 实测）→ **一律 `convert_pdf` 转图 → 你多模态读页拆题 → `ingest_items`**；文字层文本仅辅助（题号定位/纯文字题） |
| **PDF（扫描/图片型）** | 无文字层 | `convert_pdf` 转图 → 你多模态读页拆题 → `ingest_items` |
| **图片（单/多张，含手拍）** | 后缀 | 你多模态直读拆题（完整转写题面为 markdown+$LaTeX$，整图/裁图挂 images.local_path）→ `ingest_items` |
| **预处理 JSON** | 契约字段 | 校验字段 → 直接 `ingest_items`（前置信息原样落库、不被打标覆盖） |
| **科学题（任意上述形态）** | `subject_root`=901..906 | 同上任意路径 + `subject_root` 指科学根 + `kp_id` 用 `resolve_kg` 查科学树叶子 + `free_tags` 落科学维度 |

**锚定 kp_id 怎么来**：调 `resolve_kg(subject_root, query=关键词)`，从返回里挑 `is_leaf=true` 的最贴切叶子。🔴 **kp_id 必须是查出来的真实叶子 id，严禁编造**（编造的 id 底座会静默丢关系）。

**多模态拆题铁律**（PRD §11）：读图拆出的题，**你自己逐题核对题数 + 抽查题面**再入库；分批时主 agent 汇总核对。位置序号（items 数组下标+1）= 入库 sort，别依赖原卷题号（遇杂散「N.」会错位）。

**灌库收尾铁律**：每灌完一卷/一批，跑 `verify_ingest(paper_id=...)` 验证题干来源前缀残留=0（ingest_items 已自动剥，但验证不可省）。

---

# 工具集（Claude ↔ 平台契约）

## 身份 / 读
| 工具 | 作用 |
|---|---|
| `login(username, password)` | 真账号登录注入身份（token 不回吐明文） |
| `list_kg_tree()` | 查整棵知识点树（组卷选叶子用） |
| `resolve_kg(subject_root, query?, section_num?, parent_id?, leaves_only?, limit?)` | 🔴 锚定查表（数学+科学通吃）：按名称/节号查节点。**叶子=`is_leaf`（无子节点），不按 level 判**（科学 901 树 5 层、902-906 树 4 层，叶深不一）。返回 `{ok,count,nodes:[{id,name,level,parent_id,is_leaf}]}` |

## 转换（确定性预处理，无需 login、零落库）
| 工具 | 作用 |
|---|---|
| `convert_doc(doc_path, batch?)` | Word→结构化文本（OMML→$LaTeX$）+ 题图清单。返回 `{ok,text,paras,text_path,images:[{rid,local_path}]}`；真 OLE .doc → 提示另存 docx |
| `convert_pdf(pdf_path, batch?, dpi?, max_pages?)` | PDF→文字层检测+按页转图（170dpi 够）。返回 `{ok,page_count,has_text_layer,pages:[页图路径],text_layer_path?}` |
| `parse_paper_text(text? / text_path?)` | 规整卷面文本→题列表（确定性规则拆题）。返回 `{ok,count,questions:[{num,type,stem,options,answer,analyze,has_fig,source,score}],digest}` |

## 录入（写层）
| 工具 | 作用 |
|---|---|
| `format_question(question_type, stem, options?)` | markdown→blockJson（三端渲染，底座永不抛） |
| `upload_image(local_path, asset_kind?)` | 本地图直传 OSS+去重，返回 ossUrl |
| `ingest_question(...)` | 录单题（低层，多数场景用 `ingest_items` 代替） |
| 🔴 `ingest_items(items, subject_root, paper?)` | **统一入库口**：一次完成 题+图+知识点关系+打标字段+可选成卷。契约见下。内置**自动剥题干来源前缀**（剥下的进 source_raw，warnings 报告） |
| 🔴 `verify_ingest(paper_id? / question_ids?)` | **灌库后铁律验证**：题干来源前缀残留检查，每次灌完卷/批必跑，`residue_count` 必须=0 |
| `get_role_manual()` | 取本说明书全文（协议内自带；另有 MCP resource `teacher://manual/ingest-role`）——fresh agent 先调它 |

## 打标 / 组卷
| 工具 | 作用 |
|---|---|
| `label_question(...)` | 已入库题的 DNA 深打标（难度/锚/解法骨架/变式底料） |
| `compose_paper` / `create_paper` / `update_paper` | 组卷 / 按题 id 成卷 / 算分值 |

---

# 契约面板 —— IngestItem（`ingest_items` 入参）

```jsonc
// ingest_items(items: IngestItem[], subject_root: str, paper?: PaperSpec)
// subject_root: KG 教材根（数学七上="100"…；科学="901".."906"）——锚定与学科分流的唯一开关
// 🔴 未知字段一律拒绝（extra=forbid，防契约漂移）；缺 stem 单条 fail 不中断批；单题失败不回滚整批
{
  // ── 题面（必填组）──
  "stem": "题干 markdown（可含 ![](ossUrl)/![](本地路径) 占位、$LaTeX$、表格）",  // 必填
  "options": ["选项A内容", "…"],          // 无=[]（非选择题）；label 自动 A/B/C
  "answer": "", "analyze": "",            // 无=""
  "question_type": 1,                      // 1选择/2判断/3应用/4填空/5解答/6作图/7计算/8证明；null=按内容推断
  "score": 0,                              // 0=未知（成卷时未知按通值补）
  "images": [{"local_path": "本地绝对路径(工具代传OSS并替换题面同名占位)", "role": "stem|figure|analysis"}],
  //          或 {"ossUrl": "已传好的url", "assetId": 123, "role": "..."}
  // ── 前置信息（可选组：带则原样落库、不带留给后续打标）──
  "kp_id": "100002005001002",             // 主考点（resolve_kg 查的真实叶子 id）
  "secondary_kps": [],
  "difficult": 2,                          // 1基础/2中等/3较难/4压轴；null=占位2留给打标
  "err": ["计算失误"],                     // 易错点（受控7词：概念混淆/计算失误/审题偏差/隐含遗漏/分类不全/表达不规范/思路缺失）→ 越词表自动剔除+warning
  //   🔴 口径说明：轻打标的 err 落 biz_question_ai.breakthrough_points（沿七上 sync_label 惯例）；
  //   深打标 label_question 另有 hard_points(难点)/breakthrough_points(突破点) 两个语义字段——别混：
  //   录入期只有 err（轻）；深打标补 DNA 时才区分难点/突破点。
  "why": "难度一句依据",                    // → biz_question_ai.difficulty_reason
  "models": [{"model_id": "TY07", "is_primary": 1}],      // 既有解法模型链
  "new_models": [{"name": "新模型名", "category": "…", "trigger_feature": "…", "action_conclusion": "…", "difficulty_tier": 2, "freq_band": 1}],  // 提议新模型(status=2待转正)
  "scenario": "纯数学",                     // 受控：纯数学/现实生活/科学跨学科/数学文化（越词表忽略+warning）
  "free_tags": ["标签1", "标签2"],          // 自由知识标签 → biz_question_ai.tags（科学轻打标即此用法）
  // ── 溯源（可选）──
  "source_raw": "", "exam_year": "", "region_code": "国标6位", "source_type": 0,  // 类型 1中考/2模拟/3期末/4月考/5单元/6自编/9其他
  "external_key": null                     // 幂等去重键；null=按题干规范化去重（同题干复用既有 qid、只加引用、不重传图）
}
// PaperSpec（可选；不给=散题不成卷，PRD 3.3-c）
{ "name": "卷名", "category_id": "卷库目录节点id", "total_score": 100, "suggest_time": 40 }
// 返回：{ok, results:[{num, question_id, created, warnings?, reason?}], paper_id?, stats:{ok,reused,fail,img}, note?}
```

**去重（AC4）**：`external_key` 命中已存在题干 → 复用既有 qid、`created=false`、图 0 传。
**前置信息不被覆盖（AC3）**：带 kp_id/difficult/err/models 的题，落库值 = 你传的值，打标流程不改写。

---

# 跑 / 自检
前置：平台 BE 在 `.env` 的 `RUOYI_BASE_URL`（默认 :8080，A 线录入 BE）在跑；MySQL :3307（dev 库 `ai_lesson_prep`）在跑。
> ℹ️ `mcp_call.py` 已内置强制 stdout UTF-8，Windows 下无需再 set 环境变量即可正常输出中文/公式（含 `\xa0`）。若你另写脚本调工具，同样记得 `sys.stdout.reconfigure(encoding="utf-8")`。
> ℹ️ 成卷时 `PaperSpec.total_score` 仅在**所有题 score 都为 0**时按标准分铺满；只要有题带了 score（如题面解析分），则用题面分求和为卷面总分（total_score 被覆盖属预期）。

```powershell
# 装依赖（独立 venv，别和 toolkit 混）
uv venv; uv pip install --python .venv\Scripts\python.exe mcp httpx pydantic pydantic-settings anyio pymupdf pymysql lxml python-dwml
copy .env.example .env   # 填 RUOYI_USERNAME/RUOYI_PASSWORD + db_*（不入 git）

# 通用工具调用器（不重启 Claude session 即可调任意工具；gates/验收都用它）
.venv\Scripts\python.exe tools\mcp_call.py --list
.venv\Scripts\python.exe tools\mcp_call.py resolve_kg '{"subject_root":"100","query":"乘方","leaves_only":true}'
.venv\Scripts\python.exe tools\mcp_call.py ingest_items --file batch.json   # 大参数走文件
```

## 加能力 = 加一个目录（AC6 解耦）
身份层(`tools/auth.py`) / 读层(`tools/kg.py`) / 转换层(`tools/convert.py`) / 写层(`tools/ingest.py`) / 组卷(`tools/compose.py`) / 打标(`tools/label.py`) 已分离。二期照样：新增 `app/tools/<name>.py` 写 `register(mcp, client)` + `app/server.py` 加一行。`login`/鉴权/底座调用层(`app/ruoyi.py`)不动。

## 架构落位（AC8 走查面）
- **app/**（MCP server，零 LLM）：`server.py`(注册) · `ruoyi.py`(HTTP + 🔴多底座 `RuoyiCluster`：A=:8080 主底座/C=:8090 讲义懒登录，同库同账号 token 各持；toolkit=:8093 配置占位) · `config.py` · `dicts.py`(字典镜像) · `paperparse.py`(确定性拆题·单一事实源) · `docconv.py`(docx/pdf 转换) · `db.py`(🔴 pymysql 收口：模型链/难度依据/卷目录补设/KG只读查表，PRD 3.3-b) · `tools/`(工具面)。
- **tools/**（本地管线脚本，薄）：`mcp_call.py`(通用运行器) · `ingest_paper.py`/`sync_ingest.py`/`run_paper.py`/`label_runner.py` 等（调 app/ 模块，逻辑已上提）。
- 🔴 **技术债显式挂账**（3.3-b）：关系表 pymysql 是本地 Claude Code 服务态的过渡；对外开放（stdio→HTTP+鉴权）前必须 HTTP 化（A 线补端点，独立卡）。

## 边界
- 不改底座源码（纯 HTTP 代理 + 少量本地 pymysql 关系表）；本期服务本地 Claude Code、不上线。
- 不上 LangChain/LangGraph（薄适配层）；探针口径单进程单会话（stdio 单 client）。
- 科学结构化维度定版（学科多值/核心概念/素养 DB 落位）归科学线专卡，本卡录入只对齐预研现状（锚+难度+易错+自由 tags）。
