# 🧭 teacher-mcp 总手册（all 角色 · fresh-context agent 上手即用）

> 这是老师系统的操作全权代理。本地 CLI agent = 编排层，经本 MCP 把合并后的 book-server（:9090）+ toolkit（:9093）现有能力当工具调。
> 一次 `login` 通吃四条线（录题 / 录讲义 / 备课 / 举一反三）。首次上手照本文走。

## 开场三步（必做）
1. `health_check()` — 探活三依赖（ruoyi BE / toolkit / MySQL）。任何 down 先修（见文末自救表），别硬调工具。
2. `login()` — 无参走 .env admin 兜底；或 `login(username, password)` 用真账号。所有写工具都需要会话身份，落 create_by 审计。
3. `get_role_manual(role)` — 取本职线手册全文：`role="data"`（录题/录讲义）/ `"prep"`（备课）/ `"variant"`（举一反三）/ `"all"`（本文）。**开工前先读对应线手册，别凭猜调工具。**

---

## 角色 / 工具地图（43 工具，按组一行一句「何时用」）

**共享组（全角色可见，7 + health）**
| 工具 | 何时用 |
|---|---|
| `login` | 开场，注入身份 |
| `health_check` | 开场/报错时探活三依赖 |
| `list_kg_tree` | 要看整棵知识点树、选组卷叶子 |
| `resolve_kg(subject_root, query?…)` | 🔴 锚定查真实叶子 id（叶子=is_leaf，不按 level 判）——kp_id 从这来 |
| `search_questions(...)` | 从题库分页检索题（备课圈题；🔴 圈自己造的题必 mine=True）；给 `batch_id`/`since` 走**找回路径** |
| `get_question(ids)` | 按 id 拉题详情（装段/核对题面前必看） |
| `my_recent_uploads(hours?)` | 🔎 一键找回本人近 N 小时录的 题/卷/讲义（按批次分组） |
| `get_role_manual(role?)` | 取角色手册全文 |

**录题组（data/ingest，写题库）**
| 工具 | 何时用 |
|---|---|
| `convert_doc` / `convert_pdf` / `parse_paper_text` | 来源预处理（Word/PDF/文本→结构化），确定性、无需 login |
| `format_question` | markdown+选项→blockJson（三端渲染） |
| `upload_image` | 本地图直传 OSS 拿 ossUrl |
| 🔴 `ingest_items(items, subject_root, paper?)` | **统一入库口**：一次完成 题+图+知识点关系+打标字段+可选成卷；给 `paper` 即成卷 |
| `ingest_question` | 单题低层录入（多数场景用 ingest_items） |
| 🔴 `verify_ingest(paper_id?/question_ids?)` | **灌库后必跑**：题干来源前缀残留必须=0 |
| `label_question(...)` | 已入库题的 DNA 深打标（难度/锚/解法骨架/变式底料） |

**录讲义组（data/lecture，写讲义）**
| 工具 | 何时用 |
|---|---|
| `convert_lecture_docx(docx, course_subject_id, mode?)` | 讲义 docx→忠实 Tiptap + KG 靶子（assist 默认/auto 清洗源/cuicui 已证伪） |
| 🔴 `save_lecture_frag(frags/ir_path, image_map?, owner?)` | 片段 IR→入库唯一口（UK=subjectId+bookId+owner 幂等覆盖） |
| `remove_lecture_frag(subject_prefix, owner?)` | 删片段（覆盖录入「先删」步）；🔴 前缀≥9 位、别用课时 12 位（连思维导图删） |
| `list_lecture_docs` / `get_lecture_content` | 查讲义目录 / 读某片段正文（text+example_qids） |

**备课组（prep，写教学安排）**
| 工具 | 何时用 |
|---|---|
| `create_teach_target` / `list_teach_targets` | 建/查教学对象（学生 target_type='student'→'0' / 班课 'class'→'1'） |
| `upsert_course_plan(plan, lessons?)` | 建/改课程计划+课次（🔴 新建必传 target_id） |
| `schedule_sessions` / `list_schedule` / `update_session` | 批量排课 / 查月历 / 改单场（改期/请假/取消/锁/改绑） |
| `build_prep_pack(lesson_id/session_id, segs)` | 装配备课包（按段填题，question_ids 字符串） |
| `render_prep_pack(pack_id)` | 备课包渲染 PDF（🔴 pack.status=备课状态唯一权威） |
| `submit_review(session_id, item_results)` | 课后回收逐题对错→家长反馈+肖像增量 |
| `get_student_profile` / `get_plan_detail` | 读对象画像/易错库 / 读计划课次蓝本（圈题依据） |
| `compose_paper` / `create_paper` / `update_paper` | 按大纲组卷 / 按题 id 成卷 / 算分值 |

**举一反三组（variant，toolkit 图，写变式题）**
| 工具 | 何时用 |
|---|---|
| `make_variants(image_url/qid, count)` | 母题起链（返 thread_id；无锚定则 need_confirm） |
| `confirm_variant_chapter(thread_id, chapter_id)` | need_confirm 时确认章节锚 |
| `generate_variants(thread_id)` | 生成变式（LLM，60-120s/轮属正常） |
| `verify_variant(thread_id, item_id)` | 变式独立验算（verdict=pass 才算过） |
| `edit_variant(thread_id, item_id, patch)` | 人工修变式题面/答案 |
| `compose_variant_figure(thread_id, item_id)` | 几何题合成配图 DSL（bbox+objects） |
| `persist_variants(thread_id, item_ids)` | 落库拿 qid（🔴 persist 前须 verify 过） |

---

## 四条典型编排流程

**① 录题入库**：来源分流（`convert_doc`/`convert_pdf`/`parse_paper_text` 或多模态直读）→ 构造 `IngestItem[]`（题干带真实 kp_id，`resolve_kg` 查真叶子）→ `ingest_items(items, subject_root)` → 🔴 **收尾必 `verify_ingest`（residue_count=0）**。全标签三件套（KG 锚 kp_id + source_raw + free_tags）尽量打齐。

**② 成卷**：同 ①，但 `ingest_items` 带 `paper={name, category_id, total_score, suggest_time}` 一步成卷；或已入库题走 `create_paper(name, question_ids)` + `update_paper` 算分。回验卷内题数走 `search_questions(exam_paper_id=)` 或 DB。

**③ 备课链**（顺序固定）：`create_teach_target`(student) → `upsert_course_plan`(挂 target_id, 建 lessons) → `schedule_sessions`(排场次, 绑 lesson) → `build_prep_pack`(段引用圈好的 qid) → `render_prep_pack` → 课后 `submit_review`。读侧 `get_plan_detail`/`get_student_profile` 提供圈题依据。

**④ 举一反三**：`make_variants` → [need_confirm 则 `confirm_variant_chapter`] → `generate_variants` → `verify_variant`(verdict=pass) → [`edit_variant` 修] → [几何题 `compose_variant_figure`] → `persist_variants` 落库。无图题落库时后端自动渲 rendered_stem，无需处理。

---

## 🔎 溯源与找回（PRD-O-005 溯源增强）

**双管道语义（看 `import_source` 一眼分清谁录的）**
- `mcp-*`（`mcp-ingest`/`mcp-data`/`mcp-all`…）= **MCP agent 机录**（本 server 录题一律打，前缀取 env `TEACHER_MCP_ROLE`）。
- `举一反三` = 举一反三**引擎**落库的变式（toolkit 自带，勿动）。
- 其余（`main`/手工导入/`textin`…，**不带 `mcp-` 前缀**）= 手工 / 其他管道。

**批次号（一次录入一个 `batch_id`）**
- `ingest_items` / `ingest_question` 每次调用返回 `batch_id`（格式 `mcp-YYYYMMDD-HHMMSS-4位随机`，可读可排序）。
- 🔴 录完记住这个 `batch_id` → `search_questions(batch_id="mcp-…")` 精确捞回本批全部题（BE 直落 `import_batch_id`）。

**两条找回路径**
- `search_questions(batch_id=…)` 或 `search_questions(since="24h"/"7d"/"2026-07-08", mine=True)`：走 DB 只读检索，
  **不依赖 stem 关键词**（故 stem_text=NULL 的变式题也不漏），返回 items 附 `import_source`/`batch_id`/`create_time`。
- `my_recent_uploads(hours=24)`：一把捞回本人窗口内的 **题（按批次分组）+ 卷 + 讲义片段**，附题库页 `view_url`。

**录入尽量带来源三要素**：`source_type`（1中考/2模拟/3期末/4月考/5单元/6自编/9其他）+ `exam_year` + `region_code`（国标6位）+ `source_raw`，
让每题除了「谁录的（import_source）」还答得出「从哪来的」，日后可溯可筛。

---

## 🔴 铁律盒子
- **id 全链路字符串**：题/对象/计划/场次/包/qid 一律以字符串传（雪花号经 JSON double 会截尾）。
- **kp_id 必查真叶子**：`resolve_kg` 挑 `is_leaf=true`，**严禁编造 id**（编造的底座静默丢关系）。
- **灌库必 `verify_ingest`**：每批/每卷收尾验来源前缀残留=0。
- **persist 前须 verify**：举一反三 `persist_variants` 前变式必须 `verify_variant` 通过。
- **写工具返回 `view_url` 给老师验收**：ingest/plan/pack/讲义写完把深链交出去。
- **测试数据带标记**：共库（ai_lesson_prep@3307 四线同写），测试题/对象名称带批次标记（如 `[PRD-O-005-TEST]`）便于清理。
- **讲义删除守纪**：`remove_lecture_frag` 前缀≥9 位、绝不用课时 12 位（连思维导图删且无法再生）。

---

## 常见错误自救表
| 现象 | 自救 |
|---|---|
| `ok:false` hint 含 9093 / toolkit down | 起 toolkit（`.venv` 起 :9093），举一反三链才可用 |
| `未登录` / `需先 login` | 先调 `login()`（.env admin 兜底或传真账号） |
| ingest 后前端「该章节暂无题目」 | MCP `ingest_items` 落库即 status='1'（我的库可见）；进**公共池**需超管 `set-public`（老师账号不可，属设计） |
| 举一反三无图题不出配图 | 正常：无图题后端自动渲 rendered_stem，无需 `compose_variant_figure` |
| BE 新接口 404 | 未用隔离 .m2 的老坑，本 clone `mvn install` 重建 |
| `resolve_kg` 查不到叶子 | 换 query 关键词或用 `section_num` 精确匹配；科学根=901..906、数学七上=100 |
