# 🎭 录入角色说明书（data 角色 · fresh-context agent 照此即可完成录入）

> PRD-O-005 重建：A/C 双线已合并为同一 book-server 服务（:9090）；单会话，一次 `login` 通吃题库/讲义两条录入线。

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

**多模态拆题铁律**：读图拆出的题，**你自己逐题核对题数 + 抽查题面**再入库；分批时主 agent 汇总核对。位置序号（items 数组下标+1）= 入库 sort，别依赖原卷题号（遇杂散「N.」会错位）。

**灌库收尾铁律**：每灌完一卷/一批，跑 `verify_ingest(paper_id=...)` 验证题干来源前缀残留=0（ingest_items 已自动剥，但验证不可省）。

---

# 工具集（agent ↔ 平台契约）

## 身份 / 读
| 工具 | 作用 |
|---|---|
| `login(username, password)` | 真账号登录注入身份（token 不回吐明文） |
| `list_kg_tree()` | 查整棵知识点树（组卷选叶子用） |
| `resolve_kg(subject_root, query?, section_num?, parent_id?, leaves_only?, limit?)` | 🔴 锚定查表（数学+科学通吃）：按名称/节号查节点。**叶子=`is_leaf`（无子节点），不按 level 判**（科学 901 树 5 层、902-906 树 4 层，叶深不一）。返回 `{ok,count,nodes:[{id,name,level,parent_id,is_leaf}]}` |
| `search_questions(..., batch_id?, since?)` | 检索题库；给 `batch_id`/`since` 走 DB **找回路径**（不依赖 stem 关键词，不漏变式题） |
| `my_recent_uploads(hours?)` | 🔎 一键找回本人近 N 小时录的 题（按批次分组）/卷/讲义 |
| `health_check()` | 探活三依赖（ruoyi BE / toolkit / MySQL），任何异常算 down 不抛 |

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
| 🔴 `ingest_items(items, subject_root, paper?)` | **统一入库口**：一次完成 题+图+知识点关系+打标字段+可选成卷。契约见下。内置**自动剥题干来源前缀**（剥下的进 source_raw，warnings 报告）。返回带 `view_url`（题库页深链） |
| 🔴 `verify_ingest(paper_id? / question_ids?)` | **灌库后铁律验证**：题干来源前缀残留检查，每次灌完卷/批必跑，`residue_count` 必须=0 |
| `get_role_manual(role?)` | 取角色说明书全文（协议内自带；另有 MCP resource `teacher://manual/data`）——fresh agent 先调它 |

## 打标 / 组卷
| 工具 | 作用 |
|---|---|
| `label_question(...)` | 已入库题的 DNA 深打标（难度/锚/解法骨架/变式底料） |
| `compose_paper` / `create_paper` / `update_paper` | 组卷 / 按题 id 成卷 / 算分值 |

## 溯源与找回（PRD-O-005 溯源增强）

**双管道来源标记**：MCP 机录一律打 `import_source="mcp-<角色>"`（`mcp-ingest`/`mcp-data`/`mcp-all`，前缀取 env `TEACHER_MCP_ROLE`）。
🔴 语义 = **不带 `mcp-` 前缀**的 `import_source`（`main`/手工导入/`textin`…）= 手工/其他管道；`举一反三` = 引擎落库的变式（勿动）。

**批次号**：`ingest_items` / `ingest_question` 每次调用生成一个 `batch_id`（`mcp-YYYYMMDD-HHMMSS-4位随机`），每题落 `import_batch_id`。
录完记住返回的 `batch_id` → `search_questions(batch_id="mcp-…")` 精确捞回本批全部题。

**找回两路**：
- `search_questions(batch_id=… )` 或 `search_questions(since="24h"/"7d"/"2026-07-08", mine=True)`——DB 只读，**不走 stem LIKE**（变式题 stem_text=NULL 也不漏），items 附 `import_source`/`batch_id`/`create_time`。
- `my_recent_uploads(hours=24)`——本人窗口内 题（按 `batch_id` 分组）+卷+讲义片段一把捞，附题库页 `view_url`。

**录入尽量带来源三要素**：`source_type` + `exam_year` + `region_code` + `source_raw`，让每题除了「谁录的」还答得出「从哪来的」。

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
  //   🔴 口径说明：轻打标的 err 落 biz_question_ai.breakthrough_points；
  //   深打标 label_question 另有 hard_points(难点)/breakthrough_points(突破点) 两个语义字段——别混。
  "why": "难度一句依据",                    // → biz_question_ai.difficulty_reason
  "models": [{"model_id": "TY07", "is_primary": 1}],      // 既有解法模型链
  "new_models": [{"name": "新模型名", "category": "…", "trigger_feature": "…", "action_conclusion": "…", "difficulty_tier": 2, "freq_band": 1}],  // 提议新模型(status=2待转正)
  "scenario": "纯数学",                     // 受控：纯数学/现实生活/科学跨学科/数学文化（越词表忽略+warning）
  "free_tags": ["标签1", "标签2"],          // 自由知识标签 → biz_question_ai.tags（科学轻打标即此用法）
  // ── 溯源（可选）──
  "source_raw": "", "exam_year": "", "region_code": "国标6位", "source_type": 0,  // 类型 1中考/2模拟/3期末/4月考/5单元/6自编/9其他
  "external_key": null                     // 幂等去重键；null=按题干规范化去重（同题干复用既有 qid、只加引用、不重传图）
}
// PaperSpec（可选；不给=散题不成卷）
{ "name": "卷名", "category_id": "卷库目录节点id", "total_score": 100, "suggest_time": 40 }
// 返回：{ok, batch_id, import_source, results:[{num, question_id, created, warnings?, reason?}], paper_id?, stats:{ok,reused,fail,img}, view_url?, note?}
//   🔴 batch_id = 本批溯源批次号；记住它，日后 search_questions(batch_id=) / my_recent_uploads 找回
```

**去重（AC4）**：`external_key` 命中已存在题干 → 复用既有 qid、`created=false`、图 0 传。
**前置信息不被覆盖（AC3）**：带 kp_id/difficult/err/models 的题，落库值 = 你传的值，打标流程不改写。

---

# 📚 书架整书录入（shelf 书系：把一本书 1:1 保真录进书架）

**与题库录入的关系**：题先经 `ingest_items` 进题库拿 qid，书架只存**引用 + 呈现覆盖**（biz_shelf_book/node/item 三表）；书可删不伤题。目标 = 书的原貌（结构不乱、版式保真、可导出打印、可课时绑定），区别于把书拆成维度的题库蒸馏。

## 工具面（小规模手工 / 修补用）
| 工具 | 作用 |
|---|---|
| `create_book(title, book_type, subject_id?, grade?, edition?)` | 新建空书。book_type: lecture讲义 / workbook练习册 / special专项 |
| `list_books` / `get_book_structure(book_id)` | 我的书列表（nodeCount/questionCount 统计）/ 整树一次拉全（目录+items） |
| `add_book_node(book_id, name, node_type, parent_id?, seq?, kp_id?)` | 加目录节点（node_type 自由值：chapter/lecture/unit/kp/sec…）。🔴 节点名卷面可见，禁内部词（层/★/素材/薄弱），只写干净知识点名 |
| `add_book_item(node_id, kind, question_id?/explain_title+explain_text)` | 节点挂内容项：kind=question 题引用（question_id 🔴 字符串）/ explain 讲解块（书自持，不引用 KG 讲义层） |
| `override_item(item_id, override)` | 书内覆盖题面呈现（不动题库原题）。角色元数据也走它：`{role:'example'|'practice', roleLabel:'典型例题', roleSeq:N}`——🔴 【典型例题N】类标记**只进元数据不进题面** |
| `bind_book_node_to_lesson(lesson_id, node_id, action)` | 书章节挂备课课次材料位（bind/unbind） |

## 🔴 整书批量（千题级）铁律
1. **别逐题走 MCP 工具**（会话扛不住）→ 走脚本管线，SOP 正本 = ai-bkb skill **「书架整书录入」**（主链十步+硬闸）+ `调度中心-录书样板/小学数学管线/README.md`（全部实战坑），此处不复述。
1b. 🔴 **图宽以 docx 声明排版尺寸（extent）为准，绝不用图片像素**（Word 高清图缩显，按像素=全书虚胖）；带图表格（媒体表）blip+VML 双通道逐张过；验收必跑 extent 终扫+媒体表审计（工具在管线根，README §19）。
2. shelf 节点/item **每次全新建不去重**（只有题按 stem_hash 幂等）——重录先 `DELETE /teacher/shelf/book/{id}`。
3. 录入用 admin，验收过闸后才转 owner；例题↔练习血缘 `mother_source='教材配套'`（与举一反三变式血缘隔离）。
4. 导出打印 = BE `POST /teacher/shelf/book/{id}/export {withAnswers}`。

---

# 🎭 讲义录入角色说明书（录入角色的姊妹角色）

**你的职责**：不管讲义从哪来、什么版式，把它录成**挂 KG 节点的讲义片段**（`biz_kg_lecture_frag`），维护者在 `/lecture-hub` 验收。与题目录入同一范式：**智能活（读文档、把讲解映射到知识点、判例题归属）你做；确定性活（忠实转换、切段、图占位、落库、去重）交给工具。**

🔴 **三条铁律**（继承三位一体）：
1. **例题零复制**：讲义里的例题**必须先走题目录入管线（`ingest_items`）拿 qid**，片段里只放 `kgExample(qid)` 节点，题面/答案不落片段。
2. **片段挂 KG，不现造知识点**：每片段 `subjectId` = 真实 `biz_subject` 节点 id（知识点 L5 / 课时 L4）；知识点从 KG 来，不从讲义现编。
3. **同一服务同一会话**：A/C 合并后讲义片段与例题都落 **:9090**（`login` 一次即可，`save_lecture_frag` 与 `ingest_items` 共用会话）。

## 路由矩阵（讲义来源 → 处理路径）

| 来源 | 版式特征 | 路径 |
|---|---|---|
| 🔴 **崔崔 docx（教师版/原卷版）** | 模块一知识精讲>分组>知识点(H4) + 模块二/三习题 | `convert_lecture_docx(mode='cuicui')` **确定性适配器**：自动切模块一为知识点讲解片段 + 覆盖闸校验（缺知识点即 FAIL）+ 返回习题模块区间。直接得 `ir_path`，上传图 → `save_lecture_frag` |
| **新/异常崔崔版式** | mode='cuicui' 覆盖闸 FAIL 或识别不出模块一 | 回退 `mode='assist'`：出忠实内容+分段+KG靶子 → **你理解式映射**（读 `raw_path` 按 `kg_targets` 重组），确认后可反哺 `cuicui_split` 配方 |
| **已清洗 docx（H3==知识点名）** | H3 就是 KG 知识点名 | `convert_lecture_docx(mode='auto')` 确定性 H3 切片 + 里外目录对齐闸 → 得 `ir_path` |
| **扫描/图片讲义（PDF 无文字层）** | pymupdf 检不到文字层 | `convert_pdf` 转图（170dpi）→ 你多模态读页、按 `kg_targets` 映射成片段 IR |
| **预处理 JSON（片段 IR）** | 已是 §契约 结构 | 校验 subjectId 真实 → 直接 `save_lecture_frag` |
| **新版式（必刷等教辅）** | 未见过 | 🔴 **先转 1-2 页样例、把「这段→哪个片段节点」映射摆给维护者确认**，对齐后才批量（版式配方每套一份，先 pilot 再投产） |

## 崔崔版式流水线（端到端）

🔴 **三类题目都进题库、都绑知识点**，讲义里只留 `kgExample(qid)` 引用（三位一体铁律）：
- **例题**（模块一知识精讲里嵌的，带【答案】/【详解】）→ 题库 + 讲解片段里 kgExample（讲练一体）
- **习题**（模块二习题精练 / 模块三巩固提升）→ 题库（按知识点可查；是否挂 kgExample 到讲义可选）

1. `login(admin)` → 讲义官方库 owner=uid1；例题/习题录题也用 admin（data_admin 管道账号）。
2. `convert_lecture_docx(docx, course_subject_id, mode='cuicui')` → **讲解 IR** + **习题原文** `exercise_raw_path`（带〖图〗+[H]标记）+ `kg_targets` + `images`。
3. **例题提取 subagent**：读讲解 IR 的节点索引 dump，标出每个知识点片段里例题的精确节点段 + 抽结构化数据。
4. **习题解析 subagent**：读习题原文，拆题 + 绑细 kp_id（用 [H4] 分组当线索）→ 结构化 items。
5. **bulk `ingest_items`**（例题批 + 习题批）→ 各拿 qid（入库即写 `biz_question_knowledge` 绑定；来源前缀自动剥进 source_raw；图走 `images[].local_path`）。
6. **splice**：讲解片段里把例题节点段删掉、原位插 `{"type":"kgExample","attrs":{"qid":qid,"knowledgeId":知识点id}}` → `save_lecture_frag`（owner 省略=官方库覆盖，幂等 updated）。图占位 `image_map={rid:ossUrl}` 回填。
7. **验收**：DB 断言讲义片段无 `【答案】/【详解】` 残留、kgExample.qid 全在 biz_question；`/lecture-hub` 点课时看渲染。
8. **排队 DNA 打标**：题目进库后批量 `label_question`（难度表驱动/解法/易错/变式底料）——与录入解耦。

> 🔴 智能只在第 3/4 步（LLM 解析题目结构 + 绑点）；转换/切段/落库/splice 全确定性。不评难度不判易错（留给第 8 步打标）。

## 讲义工具集

| 工具 | 作用 |
|---|---|
| `convert_lecture_docx(docx_path, course_subject_id, book_id?, batch?, mode?)` | 讲义 docx→片段 IR / 原料。`mode='cuicui'`(🔴崔崔版式确定性适配器，出知识点讲解 IR+覆盖闸+习题原文 exercise_raw_path) / `'assist'`(默认，出原料给理解式映射) / `'auto'`(H3==知识点的清洗源，确定性 H3 切片+对齐闸)。图出 `〖图:rId〗` 占位+rid |
| 🔴 `save_lecture_frag(ir_path? / frags?, book_id?, image_map?, owner?, allow_toc_fail?)` | 片段 IR→:9090 upsert 入库（唯一入库口，幂等覆盖 updated）。`image_map={rid:ossUrl}` 回填图；`__UNMATCHED__` 片段默认拦截（对齐闸）；省略 owner=登录者（admin=官方库）。返回带 `view_url`（讲义浏览页深链） |
| `remove_lecture_frag(subject_prefix, book_id?, owner?)` | 删某 owner 某前缀下的讲义片段（覆盖录入的「先删」步）。🔴 前缀到课时(12位)会连课时级思维导图一并删——想保留导图就别用课时前缀 |
| `list_lecture_docs(book_id?)` / `get_lecture_content(subject_id, book_id?, owner?)` | 讲义读侧：查目录 / 读某知识点讲义正文（text + example_qids） |

> 复用题目录入的工具：`ingest_items`（例题/习题过题库拿 qid）、`upload_image`（图传 OSS）、`resolve_kg`（查知识点 id）、`format_question`。

## 契约面板 —— 片段 IR（`save_lecture_frag` 入参）

```jsonc
// save_lecture_frag(frags: LectureFrag[], book_id, image_map?)
{
  "subjectId": "901001002001003",       // 挂载锚：真实 biz_subject 节点 id（知识点L5/课时L4）
  "title": "长度的测量",                 // 默认取知识点名
  "contentJson": {                       // Tiptap doc：讲解段/表(带背景色)/图(src=ossUrl)/kgExample(qid)/思维导图
    "type": "doc",
    "content": [ /* heading/paragraph(留 bold/color mark)/table/image/kgExample 节点 */ ]
  },
  "status": "0"                          // 0正常/1草稿
}
// owner 不传 = 登录者（admin→官方库）；返回 {ok, owner, results:[{subjectId, action:"created|updated"}], stats, unresolved_images, view_url}
```
**允容策略**：`subjectId` 必须真实存在于 biz_subject（BE 校验，不存在则该片段 fail）；`kgExample.qid` 必须在 biz_question；单片段失败不回滚整批。

---

# 边界
- 不改底座源码（纯 HTTP 代理 + 少量本地 pymysql 关系表）；本期服务本地 CLI agent、不上线。
- 🔴 技术债显式挂账：关系表 pymysql（模型链/难度依据/卷目录/KG查表）是本地服务态的过渡；对外开放（stdio→HTTP+鉴权）前须 HTTP 化。
