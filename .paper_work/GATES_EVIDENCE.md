# PRD-C-208 gates 证据留档（2026-07-03）

全部走 `tools/mcp_call.py`（stdio harness，不重启 session）+ pymysql 断言。BE=:8080(A线) 全程未重启。

| gate | 样例文件 | 断言脚本 | 结论 | 关键 qid/paper |
|---|---|---|---|---|
| G1 | `.paper_work/g1_item.json` | 内联 | ✅ | q2072752606727467010（dim1_kp/难度/知识点/err/why/tags/scenario 全落） |
| G2 | `.paper_work/g2_items.json` | 内联（paper 计数差） | ✅ | 5 题散题，paper 75→75 不变 |
| G3 | `.paper_work/docx_route.py`（七下平行线卷） | 内联 | ✅ | paper 96，20题=digest，cat=3001003001，图31行 |
| G4 | `.paper_work/g4a_items.json`+`g4b_items.json` | `.paper_work/g4_assert.py` | ✅ | 文字层降级转图2 + 扫描2，公式含$..$，扫描题带图 |
| G5 | `.paper_work/g5_item.json` | `.paper_work/g5_assert.py` | ✅ | q2072756020312109057，手拍压轴完整转写+图传OSS |
| G6 | `.paper_work/g6_item.json` | 内联逐字段 | ✅ | q2072753512990732289，17字段全等，新模型TY26 status=2 |
| G7 | `.paper_work/g7_item.json` | `.paper_work/g7_assert.py` | ✅ | q2072756784342331394，锚904004002013+难度+free_tags |
| G8 | 重跑 g1_item.json | 内联 | ✅ | created=false，qid复用，img=0 |
| G9 | `.paper_work/g9_equiv.py`（同步2+日常1份） | 逐题字节diff | ✅ | 新旧parse 100%等价（拦下NBSP漂移bug） |

复跑：`cd teacher-mcp && .venv/Scripts/python.exe .paper_work/g9_equiv.py`（离线，最能证等价）；
其余 gate 需 BE:8080 + MySQL:3307 在跑，`.venv/Scripts/python.exe tools/mcp_call.py ingest_items --file .paper_work/gN_*.json`。
