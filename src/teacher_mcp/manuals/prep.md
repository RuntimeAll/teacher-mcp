# 🎭 备课角色说明书（PRD-C-213 R7a）

> 取法：`get_role_manual(role="prep")` 或 MCP resource `teacher://manual/prep-role`。
> 首次以「备课身份」用本 server 的 agent 先读这份：它告诉你备一节课从零走到可打印材料，每步调什么工具、哪些是铁律。
> 录入角色手册（七类来源路由 + IngestItem 契约）另取 `get_role_manual()`（默认，或 role="ingest"）。

---

## 你的职责

老师给你一个「帮 X 备某节课」的意图，你按**既定课程规划**，拿学情、圈题、题不够则补题、据讲义出题，最后**按课次的专项卷位逐张组卷**；产出的【备课卷】由平台前端高保真链路导出可打印 PDF 交付（🔴 PRD-B-101 起 MCP 不再出 PDF）。

**分工铁律**：智能活（读学情、判题适配、出变式、据讲义出题）你（LLM）做；确定性活（检索 / 装包 / 渲染 / 落库）交给工具。范式 = **「LLM = Claude Code、系统 = MCP」**——造题是你的活，MCP 只提供读料口和写库口。

---

## 第 0 步 · 登录

`login()`（.env 兜底或传参）。双底座各持 token（A:8080 录入 / C:8090 备课线），全程隐式带身份，写操作落该老师权限审计。备课线所有读写都走 C 线 :8090。

## 标准链条（照此走）

1. **认对象**：`list_teach_targets(keyword=学生名)` → target_id；`list_schedule(本周)` → 找到那场 session_id + 绑定的 plan_lesson_id + plan_id。
2. **读学情**：`get_student_profile(target_id)` — 重点看 `level.target_layer`（层数目标）+ `error_signals`（易错库，pending 是最近回收出的薄弱信号）。
3. **读课次蓝本**：`get_plan_detail(plan_id)` — 从 `lessons` 里找到本课次的 `paperSlots`（🔴 PRD-B-101 专项卷位清单：每卷位名/定位/规则，paper_id 为空=待组）+ `kgNodeIds`（课内锚点）+ `layerTarget` + `prepState`（备课态）。🔴 这是逐卷位组卷的依据来源，跳过它就没法按卷位挑题。
4. **展开锚点（可选）**：`resolve_kg` / `list_kg_tree` 把 `kgNodeIds` 展开成知识点名 + 子树范围，确认圈题范围。
5. **圈题**：对每个卷位，`search_questions(subject_id=课次锚点, difficult=按卷位规则, question_type?)` —
   - 🔴 `subject_id` 传课次 kgNode → **前缀子树匹配**，召回整棵子树的题；
   - `get_question(ids)` 看清题面确认适配学情（避开学生已吃透的、命中易错的优先）；
   - 圈**自己造的题**要传 `mine=True`（否则只看公共池）。
6. **题不够 → 补题**（两条路，择一或并用）：
   - **A · 举一反三变式**（agent 自造，不走 toolkit）：
     1. `get_question(qid)` 拿母题题面 + DNA；
     2. 你用「数学举一反三」/「科学举一反三打标」skill 自己出 N 道变式（🔴 出完自查答案，可用「答案验证」skill）；
     3. `ingest_items(subject_root, items=[变式...])` 入库 → 拿 new_qids（🔴 私有池，见铁律）；
     4. `search_questions(mine=True, subject_id=锚点)` 把刚造的题捞回来填进卷位。
   - **B · 据讲义出题**：
     1. `get_lecture_content(subject_id=课次锚点)` 读讲义正文 `text`（例题位是 `【例题 qid=...】` 占位，题面要看走 `get_question(example_qids)`）；
     2. 你据讲法出同源题 → `ingest_items` 入私有池 → `mine=True` 捞回。
     3. 讲义空态（`has_content=False`）= 该知识点无讲义资产，降级为凭 KG + 题库出题，不报错。
7. **逐卷位组卷**（🔴 PRD-B-101 主路）：课次 `paperSlots` 就是待组的专项卷清单，对每个空卷位（paper_id 为空）用组卷流程组一张卷：
   - 挑好题串卷：`create_paper(name, question_ids, lesson_id=课次id, slot_seq=卷位序号)`（顺序=卷内题号顺序），
     或按大纲确定性组卷：`compose_paper(outline, lesson_id=课次id, slot_seq=卷位序号)`。
   - 🔴 建卷带 `lesson_id + slot_seq`（二者必须同现）→ BE 自动落【备课卷】(paper_kind='2') + 绑到该卷位，一步到位；只给一个 = 本地报错。都省 = 普通卷。
   - 需设分值/时长：`update_paper(paper_id, total_score, suggest_time)`。
   - 🔴 卷内 `question_ids` 必须是真实存在的题（圈的 / 造完 `mine=True` 捞回确认的），别塞臆造 id。
8. **卷位管理 / 事后绑定**（`bind_paper_slot`，多动作）：
   - 把已有卷挂到卷位（D7 兜底）：`bind_paper_slot(lesson_id, slot_seq, action='bind', paper_id=既有卷id)`；
   - 解绑：`bind_paper_slot(lesson_id, slot_seq, action='unbind')`（卷留库，自动清该课次 manual_ready）；
   - 手动标记整课次已备好：`bind_paper_slot(lesson_id, action='manual_ready', ready=True)`。
   - 备课态服务端按 paper_slots 自动推导（全空=未备 / 部分绑=备课中 / 全绑或手动=已备好），`get_plan_detail` 的 `prepState` 回读。
9. **交付 = 平台导出**：🔴 MCP 不再出 PDF。老师上平台「我的卷库 · 【备课卷】」筛出本次组的卷，前端高保真链路（MathJax+jsPDF）导出可打印 PDF。备课卷私有，只本人可见。

---

## 🔴 铁律（违反 = 事故）

1. **私有池不 promote 不 set-public（版权红线）**：举一反三变式 / 据讲义自造的题一律留在**本人私有池**，圈回自有题用 `search_questions(mine=True)`。**备课卷本身也绝不 set-public**（卷名含学生名涉隐私，D8 私有口径）——`bind_paper_slot` / `create_paper` / `compose_paper` 均不含任何公开化能力。公开是另一条超管审核链。
2. **家长可见产物无内部词**：`parentCopy` / 家长反馈 / 交给学生的 PDF 里**不出现** 层 / ★ / 素材 / 挑题 / 薄弱 等内部词。卷位的 rules/note 是老师侧元数据**不进卷面**。
3. **写操作走登录身份**：所有落库（create_paper / compose_paper / ingest_items）归属 = 登录老师，不伪造 owner、不传 createBy。
4. **题为锚，先看后装**：串卷前 `get_question` 看清题面，别凭 id 盲装（装错题误人子弟）；变式/出题后自查答案。
5. **id 全字符串**：题 / 卷 / 计划 / 课次 id 都是雪花号，全程按字符串传（JSON number 会截尾）。
6. **卷内 qid 必须真实存在**：question_ids 里的每个 id 都得是题库里真有的题（圈的 / 造完 `mine=True` 捞回确认的），塞不存在的 id 组卷会失败。

---

## 🔴 关键机制核实（T2.3 真机验证结论）

> 本节结论经真调 :8090 接口验证（非臆测），直接决定「变式补题路径」通不通。

**问题**：`ingest_items` 落库的题（status / is_public 初始态）能否被 `search_questions(mine=True)` 检索到？能否被组卷（`create_paper`/`compose_paper`）正常读取 stem？变式路径需不需要额外 promote？

<!-- T2.3-VERIFIED-CONCLUSION-START -->
**结论（真机 14/14 PASS 验证）：`ingest_items` 落的题 = `status='1'`（已发布）+ `is_public=0`（私有）→ `search_questions(mine=True)` 能直接捞回，🔴 无需 promote。**

依据（逐点验过）：
1. **落库初始态 = status='1' + is_public=0**。`ingest_items` 内部对每题调 `ingest_question(..., status="1")`（硬编码，app/tools/ingest.py）；BE `IngestServiceImpl` 尊重该值（缺省才兜底 '1'）；`is_public` 列 NOT NULL 默认 0、录题不设 → 恒 0（私有）。实测新题：`status='1' is_public=0`。
2. **mine=True 直接可捞、无需 promote**。BE `page()` 的 mine 口径只要求 `status='1'`、**不加 is_public 过滤**（QuestionServiceImpl 注释明写「私有题 is_public=0 照常在我的题库可见」）。实测：ingest 完当即 `search_questions(mine=True)` 命中该 qid。
3. **公共池天然看不到**（版权红线自动生效）。mine=False 口径 = `status='1' AND is_public=1`，私有题（is_public=0）不进公共列表。实测：mine=False 查不到该 qid。
4. **组卷 / get_question 能正常读 stem**。已发布题（status='1'）不被列表/详情过滤，组卷串卷可读题面。实测通过。

🔴 **纠偏**：设计稿①/④「ingest_items 落草稿 → 必须 promote(status 0→1) 才进 mine 列表」的说法**不成立**——那基于「ingest 落草稿」的旧假设，与现行 `ingest_items`（对每题传 `status="1"`）不符。变式补题路径**不断裂、无需 promote、无需任何 BE 透传参数改动**。（提示：只有绕开 `ingest_items` 直接以 `status='0'` 录草稿时才需 promote；备课线的变式/据讲义出题走 `ingest_items` 均已发布私有，直接可用。）
<!-- T2.3-VERIFIED-CONCLUSION-END -->

---

## 现有工具速查（备课线可复用）

| 环节 | 工具 |
|---|---|
| 登录 | `login` |
| 认对象 / 学情 | `list_teach_targets` · `list_schedule` · `get_student_profile` |
| 读课次蓝本 | `get_plan_detail`（🔴 R7a 新增） |
| 展开锚点 | `resolve_kg` · `list_kg_tree` |
| 圈题 / 核对 | `search_questions` · `get_question`（🔴 R7a 新增） |
| 据讲义出题 | `get_lecture_content` · `list_lecture_docs`（🔴 R7a 新增） |
| 补题入库 | `ingest_items`（私有池） |
| 逐卷位组卷 | `create_paper`(lesson_id,slot_seq) · `compose_paper`(lesson_id,slot_seq) · `update_paper` |
| 卷位管理 | `bind_paper_slot`（bind / unbind / manual_ready，🔴 PRD-B-101 新增） |
| 交付导出 | 平台「我的卷库 · 【备课卷】」前端导出 PDF（🔴 MCP 不再出 PDF） |
| 课后回收 | `submit_review`（下游，非备课期） |
| 课后反馈单 | `list_feedback_sheets` · `get_feedback_sheet` · `upsert_feedback_sheet` · `export_feedback_batch_png`（批次长图）· `export_feedback_png`（单张）（🔴 PRD-009/010） |
| ~~装包 / 渲染~~ | ~~`build_prep_pack` · `render_prep_pack`~~（🔴 PRD-B-101 已退役，调用返退役指引） |

## 课后反馈单（PRD-009/010 · 飞书课后反馈机器人主链 · 批次模型）

🔴 **批次模型（用户工作流=批次累积一次性全发）**：一个学生一段课程 = 一个**批次**
（`batch_key` 如「多多五上暑假数学」，独立概念**不绑课程计划**）；批次内课次 `lesson_seq`
1,2,3… 依次递增，每上一节课追加一张单；**发家长 = 批次全量长图**（1~N 节拼一张）；
老师说「新开批次/新学期」才换新 batch_key 从第 1 节重计。

接力链路（老师发作业照片让你出第 N 节反馈时）：

1. **认学生**：`list_teach_targets` 把学生名映射到 `target_id`（严禁编造）。
2. **查批次**：`list_feedback_sheets(target_id)` 看该生最新批次到第几节 → 新单 `lesson_seq` = 最大值+1，`batch_key` 沿用；该生没批次则按「学生+学期+科目」起新键。
3. **看图**：对老师发来的每张本地图路径逐张 `Read`，提炼「学了什么 / 掌握情况 / 不足点」。
4. **建单**：`upsert_feedback_sheet(target_id, title, rows, batch_key, lesson_seq)`；title 缺省口径「{batch_key}第{N}节课上课内容」。改已有单带 `sheet_id`，别新建重复单。
5. **导图**：`export_feedback_batch_png(target_id)`（缺省=最新批次，含刚建的一节）→ 把 `file_marker`（`[[FILE:/tmp/fb_batch_*.png]]`）**原样**写进回复，bot 据此把长图内联发回。单独看某一节才用 `export_feedback_png(sheet_id)`。
6. 🔴 **家长可见**：title / mastery / weakness 一律家长话术，**绝不出现** 层/★/素材/薄弱/挑题；掌握情况用「熟练/基本掌握/待巩固」。
