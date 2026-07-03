# 讲义录入流程（崔崔科学讲义 → 平台，权威参考）

> 把一份崔崔老师科学讲义 docx，录成平台里「可浏览、可组书、题目全绑知识点」的结构化资产。
> 本文 = 完整流程 + 架构 + 决策 + 踩坑 + 验收。代码在 `teacher-mcp/`（MCP 工具 + `pipeline/` 脚本）。

---

## 0. 讲义录入是什么（数据模型）

一份讲义 = 挂在 KG（知识图谱 `biz_subject`）节点上的**片段** `biz_kg_lecture_frag`：
- **一个知识点一片段**（`subject_id` = 知识点 L5 节点 id；课时 L4 也可挂片段）。某课时的完整讲义 = 该课时下所有片段按树序汇聚。
- 片段内容 = Tiptap JSON（讲解文字/表格/配图/`kgExample` 节点）。
- 🔴 **三位一体铁律**：题目（例题/习题）**只存 `kgExample(qid)` 引用**，题面/答案/详解在**题库** `biz_question`，绝不复制进讲义。

```
KG(biz_subject) ──挂── 讲义片段(biz_kg_lecture_frag) ──kgExample(qid)──> 题库(biz_question)
   知识点树                讲解+例题卡                        题目本体+知识点绑定
```

三类内容的去向：

| 崔崔 docx 里的 | 去哪 | 讲义里怎么体现 |
|---|---|---|
| **模块一·知识精讲** 的讲解 | 讲义片段（讲解正文） | 讲解文字/表/图 |
| **模块一里嵌的例题**（带【答案】【详解】） | 题库 + 讲义 | `kgExample(qid)` 卡片（讲练一体） |
| **模块二习题精练 / 模块三巩固提升** | 题库（绑知识点） | 默认不进讲义（题库按知识点可查；要挂也可 kgExample） |

---

## 1. 崔崔版式配方（30+ 册通用，已固化）

崔崔七上科学 docx 版式统一（`app/lectureconv.py::cuicui_split`）：
```
模块一：知识精讲 (H3)
  ├─ 长度测量 / 体积测量 (H3 分组头，丢弃)
  │    ├─ 测量的含义 / 长度的单位 … (H4 = KG 知识点!) ← 讲解片段按此切
  │    └─ 例题(题面+【答案】+【详解】)               ← 拆去题库，讲义留 kgExample
模块二：习题精练 (H3)   ← 题目，去题库
模块三：巩固提升 (H3)   ← 题目，去题库
```
- 🔴 知识点在 **H4** 不在 H3；纯 H3 切片对不上（207 老片段能按 H3 切是因为切的是已清洗的 biz_kg_doc）。
- 知识点标题**统一提到 H3**（与库内片段一致，`/lecture-hub` 汇聚按 H3 认知识点）。
- **覆盖闸**：KG 该课时每个知识点都要有讲解片段，缺则 FAIL（版式偏离崔崔标准的册被拦，回退人工）。

---

## 2. 每册 8 步流程

> 智能只在第 4/5 步（2 次 subagent：解析题目结构+绑点，可并行）；其余全确定性工具。全程 admin 登录（讲义官方库 owner=uid1，录题也 admin=data_admin 管道账号）。

```
1. convert_lecture_docx(docx, course_subject_id, mode='cuicui')
     → 讲解 IR(<batch>_ir.json：10 知识点片段，标题提 H3，覆盖闸)
     → 习题原文 exercise_raw_path(模块二/三，带〖图〗+[H]标记)
     → kg_targets / images(rid→本地图)

2. 讲解入库：讲解 IR 逐图 upload_image → save_lecture_frag(image_map 回填)   # :8090 官方库，幂等 upsert

3. python pipeline/dump_lecture_nodes.py <讲解IR> <nodes.txt>               # 节点索引 dump

4. 【例题提取 subagent】prompts/extract_examples.md（读 nodes.txt）
     → examples.json：每例题的精确节点段 + 题面/答案/详解/type/来源/图
   【习题解析 subagent】prompts/parse_exercises.md（读 exercise_raw_path）  # 与例题并行
     → parsed_items.json：习题结构化 + 绑细 kp_id

5. 题库入库（两批都 ingest_items，入库即写 biz_question_knowledge 绑定 + 来源自动剥 + 图上传）：
     · 习题：python pipeline/ingest_exercises.py <parsed_items.json> <docx> <subject_root>
             → ingest_batch.json，再 mcp_call ingest_items --file 它
     · 例题：python pipeline/splice_examples.py <examples.json> <docx> [book_id] [subject_root]
             ↑ 这一步入库例题拿 qid **并**把讲解片段里例题节点段删掉、原位插 kgExample、重存

6. 验收（DB 断言）：
     · 讲义片段无 【答案】/【详解】 残留（三位一体铁律）
     · kgExample.qid 全在 biz_question
     · 每知识点都有讲解片段（覆盖闸）

7. /lecture-hub 点该课时看渲染：讲解 + 例题卡 + 表格颜色 + 配图忠实

8. 排队 DNA 打标：题库题目批量 label_question（难度表驱动/解法/易错/变式底料）——与录入解耦，攒批再跑
```

> 🔴 **打标状态机**（`biz_question`，录入管道自动写对，无需额外动作）：`label_status` 0=未标(AI未处理) / 1=AI已标 / 2=已审 / 3=存疑；另有 `annotate_status`(0未标/1已标全/2部分)、`label_confidence`、`labeled_by/at`。
> **待打标队列 = `WHERE label_status=0`**；批量打标一批一行记 `biz_label_job`（total/done/failed 进度追踪），打完置 1 + labeled_by 记模型名。录入落库即 label_status=0——"AI 未处理"是显式状态，不靠记忆。

## 3. 关键决策（已拍板）

| 事项 | 决策 | 原因 |
|---|---|---|
| 讲义官方库账号 | **admin(uid1)** | BE 规矩：官方片段仅 uid1 可改；`.env` 已切 admin/admin123 |
| 覆盖录入 | **upsert 幂等**（save 即全替换，updated 不重复） | 无需先删；UK=subjectId+bookId+owner |
| 课时思维导图 | **不要了**（后续册不保留） | 本不在 docx 里、来自知识梳理；用户 2026-07-03 定 |
| 难度/易错/解法 | **录入期不碰，留第 8 步打标** | 难度表驱动、LLM 不自评（见 [[c102-variant-upgrade-strategy]]） |
| 智能放哪 | MCP 零 LLM；拆题/绑点 = 驱动 agent（subagent） | PRD-C-1000 铁律 |

## 4. 踩过的坑

- 🔴 **例题必须拆出题库**（曾漏、被用户抓）：cuicui 早期把模块一原样搬 → 例题原文(含答案详解)塞进讲义，违反铁律。修法 = 例题提取 subagent 标节点段 → 入题库 → splice kgExample。
- **混合节点**（讲解句"③计算…"和题面"如图…"同一段落）：例题用 `keep_prefix_runs` 保住前 N 个讲解 run。
- **纯讲解片段**（名师解读无答案详解）不抽例题（001/002/004/006/007 这类）。
- **别 remove 到课时前缀(12位)**：会连课时思维导图一并删。
- **确定性切题不可靠**（多段详解跨块串位、模块三无分组）→ 交 subagent 解析，别用死正则。
- 忠实转换器等价回归基线在 `reference_207/`（146 块 144 字节等）。

## 5. 成本 + 现状

- **每册 ≈ 150K token**：例题提取 subagent ~66K + 习题解析 subagent ~76K（可并行）+ 工具若干 K。30 册可分批/并行推。
- **1.2.1（长度·体积的测量）已完整录入**：10 讲解片段（教师版覆盖）+ 9 例题（kgExample）+ 22 习题（绑知识点）+ 铁律 0 残留。**课件导入 2026-07-03 起暂停**（用户定），流水线沉淀待续。

## 6. 文件清单（`pipeline/`）
- `dump_lecture_nodes.py` — 讲解 IR → 节点索引 dump
- `prompts/extract_examples.md` / `prompts/parse_exercises.md` — 两个 subagent prompt 模板
- `ingest_exercises.py` — 习题 → ingest 批
- `splice_examples.py` — 例题入库 + splice kgExample + save

> MCP 工具契约、IngestItem 契约、忠实转换细节见上层 `teacher-mcp/README.md` 的「讲义录入角色说明书」。
