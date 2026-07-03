# 崔崔讲义录入流水线（每册重复，可复用）

> 把崔崔科学讲义 docx 录成「讲解片段（挂 KG）+ 例题/习题（进题库、绑知识点、讲义里 kgExample 引用）」。
> 智能只在 2 个 subagent 步（LLM 解析题目结构+绑点，可并行）；其余全确定性。三位一体铁律：题面/答案只在题库，讲义只存 kgExample(qid)。

## 每册 8 步

```
0. login(admin)                                   # 官方库 owner=uid1；录题也 admin
1. convert_lecture_docx(docx, course_id, mode='cuicui')
     → 讲解 IR (.lecture_work/<batch>_ir.json)     # 10 知识点片段(标题提H3)+覆盖闸
     → 习题原文 exercise_raw_path                   # 模块二/三，带〖图〗+[H]标记
     → kg_targets / images
2. 讲解 save：先把讲解 IR 上传图 + save_lecture_frag 落库（见主 README 流水线第 2 步/或 run_ingest 脚本）
3. python pipeline/dump_lecture_nodes.py <讲解IR.json> <nodes.txt>
   → 派「例题提取 subagent」(prompts/extract_examples.md)  # 出 examples.json：例题节点段+数据
4. 派「习题解析 subagent」(prompts/parse_exercises.md)      # 出 parsed_items.json：习题结构化
5. 习题入库：python pipeline/ingest_exercises.py <parsed_items.json> <docx> <subject_root>
   → 生成 ingest_batch.json，再 mcp_call ingest_items --file 它
6. 例题入库+splice：python pipeline/splice_examples.py <examples.json> <docx> [book_id] [subject_root]
   → 入库例题拿 qid + 讲解片段删例题段插 kgExample + save
7. 验收：DB 断言讲义无【答案】/【详解】残留 + kgExample.qid 全在 biz_question；/lecture-hub 看渲染
8. 排队 label_question 批量 DNA 打标（难度/解法/易错/变式；与录入解耦）
```

## 文件
- `dump_lecture_nodes.py` — 讲解 IR → 节点索引 dump（给例题 subagent 定位节点段）
- `prompts/extract_examples.md` — 例题提取 subagent prompt 模板（替换 {占位}）
- `prompts/parse_exercises.md` — 习题解析 subagent prompt 模板
- `ingest_exercises.py` — 习题 parsed_items → 补图路径 → ingest_batch.json
- `splice_examples.py` — 例题入库 + 从讲解删例题段插 kgExample + save（单 MCP 会话）

## 每册成本（1.2.1 实测）
- 例题提取 subagent ~66K token；习题解析 subagent ~76K token；工具调用若干 K。
- 每册 ≈ 150K token（两次 subagent 为主，可并行）。30 册可分批/并行推。

## 关键约定（踩过的坑）
- 讲义官方库 owner=uid1(admin)，仅 uid1 可改；`.env` 已切 admin。
- 覆盖录入用 upsert（幂等 updated）即全替换；**别 remove 到课时前缀(12位)**——会删课时思维导图(kgMindmap，来自知识梳理非 docx)。
- 起始节点混了讲解+题面时用 `keep_prefix_runs` 保住讲解句。
- 纯讲解片段（名师解读无答案详解）不抽例题。
- 不评难度/不判易错（留第 8 步打标，难度表驱动）。
