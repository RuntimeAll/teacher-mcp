# -*- coding: utf-8 -*-
"""G13 · 苏俊宇案例全链复刻驱动脚本（PRD-C-213 最重路径验收）。

不经 MCP 协议——直接 import app/tools/schedule.py 的模块级纯函数，打 C 线 :8090。
取材正本：
  workplace/.prd_ccw/PRD-C/PRD-C-213/artifacts/模块设计/04-暑期奥数大纲-苏俊宇-v3.md
  workplace/.prd_ccw/PRD-C/PRD-C-213/artifacts/模块设计/07-第1次课备课包-苏俊宇.md

链路（任一步 FAIL 即退出码 1；R1a/R1b 修复轮后口径）：
  ① create_teach_target  建档（🔴 R1a 新字段：grade_no=4/grade_year=2026/textbook_edition='2'/subject='1'）
  ② upsert_course_plan   暑期计划 + 13 课次（🔴 R1a·S1 必传 target_id 归属）
  ③ schedule_sessions    批量 13 场（日期钉死 2026），断言 created=13 + 绑定序=lesson_seq 序
  ④ build_prep_pack      第 1 次课三段（题 id 从 POST /teacher/question/page 取真实公开题 2+20+9=31，不编造；
                         课内段带 groups 段内分组 BUG-004）
  ⑤ render_prep_pack     🔴 断言单文件（BUG-010）：1 个 artifact 且 pages>=3（三段起新页）
  ⑤b update_session      第 1 场标已上（mark_done）
  ⑥ submit_review        录逐题对错，断言 parent_msg 格式（R1b S5 即时生成）+ 内部词黑名单 + portrait_delta pending
  ⑦ get_student_profile  断言 error_signals 出现新 pending 信号
  ⑧ rebind-plan 冒烟     建计划 B → 换绑验 {rebound, unbound} → 换绑回计划 A 复位

幂等策略（注明·自行定夺）：开头 list_teach_targets 按「苏俊宇」查重——已存在则**加时间戳后缀**
另建新档（保证断言确定性，不复用旧档避免旧数据污染断言）；排课若因上一轮 G13 场次「老师撞场」
一条不落，则提示并 force=True 重发一次（契约 D6 强存语义）。

跑法（🔴 需 C 线 :8090 已起 + 批1 BE 端点已实现）：
  cd D:\\workplace\\book-ai\\teacher-mcp
  .venv\\Scripts\\python.exe scripts\\g13_sujunyu.py
"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # teacher-mcp 根

from app.config import settings
from app.ruoyi import RuoyiCluster, RuoyiError
from app.tools.schedule import (
    _build_prep_pack,
    _create_teach_target,
    _get_student_profile,
    _list_teach_targets,
    _render_prep_pack,
    _schedule_sessions,
    _submit_review,
    _update_session,
    _upsert_course_plan,
)

TODAY = dt.date.today().isoformat()

# ───────────────────────── 断言小harness ─────────────────────────
_FAILED = False


def step(name: str, ok: bool, detail: str = "") -> None:
    global _FAILED
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" · {detail}" if detail else ""))
    if not ok:
        _FAILED = True
        sys.exit(1)


def info(msg: str) -> None:
    print(f"       {msg}")


# ───────────────────────── 案例基准数据（04-v3 / 07 取材）─────────────────────────
PROFILE = {
    "traits": ["非常聪明", "注意力高开低走"],
    "level": {"desc": "远超年级·5年级水准", "target_layer": "逻辑3层冲4层"},
    "env": "外部四年级奥数班课",
    "history": [
        {"topic": "行程·两次相遇", "status": "讲过未吃透", "src": "外部奥数班"},
        {"topic": "复杂和差倍", "status": "讲过未吃透", "src": "外部奥数班"},
        {"topic": "盈亏/鸡兔等老专项", "status": "讲过未吃透", "src": "外部奥数班·滚动复练清旧账"},
    ],
    "error_signals": [
        {"tag": "计算粗心", "evidence": "会做的题算错，验算回查意识弱", "session_id": None,
         "ts": TODAY, "by": "teacher", "status": "confirmed"},
        {"tag": "概念辨析·周长vs面积", "evidence": "周长/面积意义混淆", "session_id": None,
         "ts": TODAY, "by": "teacher", "status": "confirmed"},
        {"tag": "倍数基准抓错", "evidence": "和差倍问题里把倍数基准量抓错", "session_id": None,
         "ts": TODAY, "by": "teacher", "status": "confirmed"},
        {"tag": "算式意义判断", "evidence": "算式意义辨析题易错", "session_id": None,
         "ts": TODAY, "by": "teacher", "status": "confirmed"},
    ],
}

DEFAULT_SEG_TEMPLATE = [
    {"name": "思维题", "style": "开场1道·单点突破·一题一坑", "topic": ""},
    {"name": "奥数专项", "style": "书挑题·★分层", "topic": ""},
    {"name": "课内同步", "style": "收尾过关·简单不费脑", "topic": ""},
]


def _seg(topic_special: str, topic_inner: str) -> list:
    """一课次的三段模板：专项 topic + 课内同步 topic（契约：课内主题落第三段 topic）。"""
    return [
        {"name": "思维题", "style": "开场1道·单点突破·一题一坑", "topic": ""},
        {"name": "奥数专项", "style": "书挑题·★分层", "topic": topic_special},
        {"name": "课内同步", "style": "收尾过关·简单不费脑", "topic": topic_inner},
    ]


# 13 课次（04-v3 §B 内部表 + §A 家长表逐行抄）
LESSONS = [
    {"lesson_seq": 1, "title": "统筹与最值", "lesson_type": "0",
     "source_ref": "学而思第10+11周 合并（第10周简单题多→两周挑核心题合成一课）",
     "thinking_action": "全局比较（统筹）＋极端思考（最值）", "layer_target": "2→3",
     "parent_copy": "怎么安排最省时间、怎么拿到最多最少（统筹与最值）",
     "seg_template": _seg("统筹与最值", "大数的认识·会读会写")},
    {"lesson_seq": 2, "title": "定义新运算", "lesson_type": "0",
     "source_ref": "学而思第12周", "thinking_action": "规则抽象与迁移", "layer_target": "3",
     "parent_copy": "新符号新规则的运算（定义新运算）",
     "seg_template": _seg("定义新运算", "大数的认识·改写和近似数")},
    {"lesson_seq": 3, "title": "错中求解", "lesson_type": "0",
     "source_ref": "学而思第13周 全讲——直击计算准确线",
     "thinking_action": "逆向＋验算回查意识", "layer_target": "3",
     "parent_copy": "算错的题倒回去找原来的数（错中求解）",
     "seg_template": _seg("错中求解", "公顷和平方千米")},
    {"lesson_seq": 4, "title": "算式之谜", "lesson_type": "0",
     "source_ref": "学而思第14+15周 合并（两讲同构→挑代表题合一课）",
     "thinking_action": "有序枚举＋逻辑排除", "layer_target": "3",
     "parent_copy": "填数字破解算式（算式之谜）",
     "seg_template": _seg("算式之谜", "角的度量")},
    {"lesson_seq": 5, "title": "等差数列巧算", "lesson_type": "0",
     "source_ref": "学而思第19+20周 提前调用（等差数列=四年级计算标配）",
     "thinking_action": "找结构、配对思想（凑整/首尾配对）", "layer_target": "3",
     "parent_copy": "一长串数怎么快速加起来（等差数列巧算）",
     "seg_template": _seg("等差数列巧算", "三位数乘两位数（上）")},
    {"lesson_seq": 6, "title": "周期问题", "lesson_type": "0",
     "source_ref": "学而思第16周", "thinking_action": "周期化归", "layer_target": "3",
     "parent_copy": "按规律循环的问题（周期问题）",
     "seg_template": _seg("周期问题", "三位数乘两位数（下）")},
    {"lesson_seq": 7, "title": "行程·两次相遇（吃透课①）", "lesson_type": "0", "tag": "吃透课①",
     "source_ref": "自制分层卷（行程·两次相遇——做过没吃透）",
     "thinking_action": "整体思想＋数形结合（盯「路程和」）；按三关判据走", "layer_target": "3→4",
     "parent_copy": "行程·两次相遇，这次彻底搞懂",
     "seg_template": _seg("行程·两次相遇", "平行四边形和梯形")},
    {"lesson_seq": 8, "title": "七月小测", "lesson_type": "1",
     "source_ref": "书·期中测(一)＋自选", "thinking_action": "", "layer_target": "",
     "parent_copy": "七月小测：考前面学的专项＋看看算得准不准"},
    {"lesson_seq": 9, "title": "综合应用", "lesson_type": "0",
     "source_ref": "学而思第17周", "thinking_action": "多模型识别与选择", "layer_target": "3→4",
     "parent_copy": "综合应用题，一题里混几种题型",
     "seg_template": _seg("综合应用", "两位数除法（上）")},
    {"lesson_seq": 10, "title": "还原问题", "lesson_type": "0",
     "source_ref": "学而思第18周", "thinking_action": "逆向还原", "layer_target": "3",
     "parent_copy": "从结果倒推回去（还原问题）",
     "seg_template": _seg("还原问题", "两位数除法（下）")},
    {"lesson_seq": 11, "title": "图形计数", "lesson_type": "0",
     "source_ref": "学而思第28周 提前调用（图形计数=三升四标配，补几何/计数空档）",
     "thinking_action": "有序枚举（分类数、不重不漏）", "layer_target": "3",
     "parent_copy": "图形里藏了多少个三角形（数图形不重不漏）",
     "seg_template": _seg("图形计数", "条形统计图")},
    {"lesson_seq": 12, "title": "复杂和差倍（吃透课②）", "lesson_type": "0", "tag": "吃透课②",
     "source_ref": "第25周素材（复杂和差倍收口）",
     "thinking_action": "画图表征（线段图）＋转化归一；三关判据", "layer_target": "4 冲击",
     "parent_copy": "难一点的和差倍，收尾吃透",
     "seg_template": _seg("复杂和差倍", "数学广角·优化＋四上收尾")},
    {"lesson_seq": 13, "title": "暑期结业测", "lesson_type": "1",
     "source_ref": "书·期末测(一)＋四上过关卷", "thinking_action": "", "layer_target": "",
     "parent_copy": "暑期结业测＋四上过关小卷"},
]

# 13 场日期钉死（2026）：7月周二 9:30-11:00、周日 13:30-15:00；8月周日 13:30-15:00
_SUN = ("13:30", "15:00")
_TUE = ("09:30", "11:00")
SESSION_SLOTS = [
    ("2026-07-05", *_SUN), ("2026-07-07", *_TUE), ("2026-07-12", *_SUN), ("2026-07-14", *_TUE),
    ("2026-07-19", *_SUN), ("2026-07-21", *_TUE), ("2026-07-26", *_SUN), ("2026-07-28", *_TUE),
    ("2026-08-02", *_SUN), ("2026-08-09", *_SUN), ("2026-08-16", *_SUN), ("2026-08-23", *_SUN),
    ("2026-08-30", *_SUN),
]

# 家长文案内部词黑名单（契约§三 parent-export/review：内部字段一律不出现）
INTERNAL_WORDS = ["层", "★", "素材", "挑题", "薄弱"]


def _get(d: dict, *keys):
    """容错取字段（camelCase / snake_case 并容）。"""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _tag_star_levels(qids20: list) -> None:
    """fixture：给专项 20 题打 star_level（★7/★★8/★★★5，dev 库测试标注，见调用处注释）。"""
    import pymysql
    stars = ["1"] * 7 + ["2"] * 8 + ["3"] * 5
    conn = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                           database="ai_lesson_prep", charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            for qid, s in zip(qids20, stars):
                cur.execute("UPDATE biz_question SET star_level=%s WHERE id=%s", (s, qid))
        conn.commit()
    finally:
        conn.close()


async def _fetch_question_ids(client, need: int) -> list:
    """从既有端点 POST /teacher/question/page 取真实公开题 id（🔴 不编造 id）。"""
    ids: list = []
    page = 1
    while len(ids) < need and page <= 5:
        resp = await client.teacher_post("/teacher/question/page",
                                         {"pageIndex": page, "pageSize": 50})
        rows = (resp or {}).get("list") or []
        if not rows:
            break
        ids += [str(_get(r, "id", "questionId")) for r in rows if _get(r, "id", "questionId")]
        page += 1
    return ids[:need]


async def main() -> None:
    # ── 0. 建 cluster（照 server.py 初始化）+ 直登 C 线 :8090 ──
    cluster = RuoyiCluster()
    try:
        u, p = settings.ruoyi_username, settings.ruoyi_password
        # 只打 C 线：不登 A 线（:8080 可能没起也无必要），直接对 C 线 client login
        client = cluster.c
        await client.login(u, p)
    except (RuoyiError, Exception) as e:
        step("0.登录 C 线 :8090", False, f"{type(e).__name__}: {e}（C 线没起？先起 book-server :8090）")
        return
    step("0.登录 C 线 :8090", True, f"user_id={client.user_id} username={client.username}")

    # ── 幂等查重：学生名「苏俊宇」已存在 → 加时间戳后缀另建（保证断言确定性，注明于文件头）──
    student_name = "苏俊宇"
    plan_name = "苏俊宇·2026暑期数学计划"
    existed = await _list_teach_targets(client, target_type="student", keyword=student_name)
    dup = [t for t in existed.get("items", []) if str(_get(t, "name")).startswith(student_name)]
    rerun = bool(dup)
    if rerun:
        suffix = dt.datetime.now().strftime("%m%d%H%M%S")
        student_name = f"苏俊宇-G13-{suffix}"
        plan_name = f"苏俊宇·2026暑期数学计划-G13-{suffix}"
        info(f"查重命中 {len(dup)} 个既有「苏俊宇」档案 → 本轮改用后缀名: {student_name}（幂等重跑）")

    # ── ① 建档（R1a 新字段：暑期录升四 = gradeNo 4 + gradeYear 2026；人教='2'、数学='1'）──
    r1 = await _create_teach_target(
        client, "student", student_name, grade_no=4, grade_year=2026,
        textbook_edition="2", subject="1", profile=PROFILE)
    target_id = r1.get("id")
    step("1.create_teach_target 建档(新字段)", bool(r1.get("ok") and target_id), f"target_id={target_id}")

    # ── ② 计划 + 13 课次（R1a·S1 必传 target_id 归属）──
    r2 = await _upsert_course_plan(
        client,
        plan={"name": plan_name, "target_type": "student", "target_id": target_id,
              "term_tag": "暑假", "year": 2026,
              "material_note": "学而思 36 周书 · 挑题制",
              "default_seg_template": DEFAULT_SEG_TEMPLATE, "status": "1"},
        lessons=LESSONS)
    plan_id, lesson_ids = r2.get("plan_id"), r2.get("lesson_ids", [])
    step("2.upsert_course_plan 计划+13课次",
         bool(r2.get("ok") and plan_id and len(lesson_ids) == 13),
         f"plan_id={plan_id} lessons={len(lesson_ids)}")

    # ── ③ 批量排 13 场 + autoBind ──
    items = [{"date": d, "start": s, "end": e} for d, s, e in SESSION_SLOTS]
    r3 = await _schedule_sessions(client, "student", target_id, items,
                                  plan_id=plan_id, auto_bind=True, force=False)
    created, conflicts = r3.get("created", []), r3.get("conflicts", [])
    if not created and conflicts and rerun:
        # 上一轮 G13 的场次导致老师撞场（同 create_by 时间重叠）→ 契约 D6 强存语义重发
        info(f"排课命中 {len(conflicts)} 个冲突（上一轮 G13 场次·老师撞场）→ force=True 强存重发")
        r3 = await _schedule_sessions(client, "student", target_id, items,
                                      plan_id=plan_id, auto_bind=True, force=True)
        created, conflicts = r3.get("created", []), r3.get("conflicts", [])
    step("3a.schedule_sessions created=13",
         bool(r3.get("ok") and len(created) == 13),
         f"created={len(created)} conflicts={len(conflicts)}")

    # 绑定序断言：按日期排序后第 i 场绑第 i 个课次（lesson_seq 序）
    def _sess_date(s):
        return str(_get(s, "sessionDate", "date") or "")
    created_sorted = sorted(created, key=_sess_date) if all(isinstance(c, dict) for c in created) else created
    bind_ok, bind_detail = True, "服务端未回绑定字段，跳过逐场比对" if not created_sorted else ""
    if created_sorted and isinstance(created_sorted[0], dict):
        bound = [str(_get(s, "planLessonId", "plan_lesson_id") or "") for s in created_sorted]
        if any(bound):
            bind_ok = bound == [str(x) for x in lesson_ids]
            bind_detail = f"绑定序 {'==' if bind_ok else '!='} lesson_seq 序"
        else:
            bind_detail = "created 明细无 planLessonId 字段，绑定序以 3a 计数为准"
    step("3b.autoBind 绑定序正确", bind_ok, bind_detail)
    session_ids = [str(_get(s, "id", "sessionId") or "") for s in created_sorted] \
        if created_sorted and isinstance(created_sorted[0], dict) else []
    first_session_id = session_ids[0] if session_ids else None
    step("3c.取第1场 session_id", bool(first_session_id), f"session_id={first_session_id}")

    # ── ④ 第 1 次课备课包三段（题 id 取真实公开题 2+20+9=31）──
    qids = await _fetch_question_ids(client, 31)
    step("4a.取 31 个真实题 id（/teacher/question/page）", len(qids) == 31,
         f"got={len(qids)} 首题id={qids[0] if qids else '-'}")
    # fixture：专项 20 题按苏俊宇案例卷 DNA 打星级（★7/★★8/★★★5）。
    # star_level 为 PRD-C-213 新列（dev 库、无其他消费者）；公开题原本无星级会全部归第一层，
    # 导致 09b 三层结构无法复刻（G10 断言口诀区+三层）——故测试前置标注，语义=老师自制卷星级。
    _tag_star_levels(qids[2:22])
    step("4a2.专项20题星级标注(★7/★★8/★★★5)", True, "fixture: biz_question.star_level")
    segs = [
        {"name": "思维题", "style": "开场·换元/单位巧思", "question_ids": qids[0:2],
         "rules": "", "note": "2 道思维题快速进入状态，一题一坑"},
        {"name": "奥数专项·最值", "style": "书挑题·★分层", "question_ids": qids[2:22],
         "rules": "第一层★7/第二层★★8/第三层★★★5选做",
         "note": "【核心口诀】和一定，两数越接近积越大、越悬殊积越小；积一定，两数越接近和越小。"
                 "造大数：大数字往高位放，后续大数字给当前较小的数。"},
        # 课内段带 groups 段内分组（BUG-004：渲染按组起小节；组并集 = 段的题）
        {"name": "课内过关", "style": "收尾过关·简单不费脑",
         "groups": [{"title": "基础过关", "question_ids": qids[22:28]},
                    {"title": "进阶", "question_ids": qids[28:31]}],
         "rules": "基础6+进阶3", "note": "大数的认识·会读会写，易错向"},
    ]
    r4 = await _build_prep_pack(client, lesson_id=lesson_ids[0], segs=segs)
    pack_id = r4.get("pack_id")
    step("4b.build_prep_pack 第1次课三段(2+20+9,课内段groups分组)", bool(r4.get("ok") and pack_id), f"pack_id={pack_id}")

    # ── ⑤ 渲染（🔴 BUG-010 单文件）：1 个 artifact 且 pages>=3（三段各起新页 → 页数至少 3）──
    r5 = await _render_prep_pack(client, pack_id, mark_ready=True)
    arts = r5.get("artifacts", [])
    total_pages = (_get(arts[0], "pages") or 0) if arts else 0
    step("5.render_prep_pack 单文件(1 artifact 且 pages>=3)",
         bool(r5.get("ok") and len(arts) == 1 and total_pages >= 3),
         f"artifacts={len(arts)} pages={total_pages}")
    for a in arts:
        info(f"artifact: seg={_get(a, 'seg')} file={_get(a, 'file')} pages={_get(a, 'pages')}")

    # ── ⑤b 第 1 场标已上（mark_done）──
    r5b = await _update_session(client, first_session_id, "mark_done")
    step("5b.update_session mark_done 标已上", bool(r5b.get("ok")), "")

    # ── ⑥ 课后回收：逐题对错（构造 错/卡 带 cause）──
    item_results = [
        {"question_id": qids[0], "seg": "思维题", "seq": 1, "result": "对"},
        {"question_id": qids[1], "seg": "思维题", "seq": 2, "result": "错", "cause": "计算"},
        {"question_id": qids[3], "seg": "奥数专项·最值", "seq": 2, "result": "错", "cause": "概念辨析"},
        {"question_id": qids[13], "seg": "奥数专项·最值", "seq": 12, "result": "卡", "cause": "策略"},
        {"question_id": qids[23], "seg": "课内过关", "seq": 2, "result": "错", "cause": "计算"},
        {"question_id": qids[25], "seg": "课内过关", "seq": 4, "result": "对"},
    ]
    r6 = await _submit_review(client, first_session_id, item_results,
                              teacher_note="高开低走明显，块二后半程注意力下滑；口诀已总结")
    parent_msg = r6.get("parent_msg") or ""
    delta = r6.get("portrait_delta") or []
    step("6a.submit_review parent_msg 以「家长您好」开头", parent_msg.startswith("家长您好"),
         f"head={parent_msg[:24]!r}")
    three_lines = all(k in parent_msg for k in ("思维题：", "同步：", "拓展奥数："))
    step("6b.parent_msg 含 思维题：/同步：/拓展奥数： 三行", three_lines, "")
    leaked = [w for w in INTERNAL_WORDS if w in parent_msg]
    step("6c.parent_msg 不含内部词【层/★/素材/挑题/薄弱】", not leaked, f"泄漏={leaked or '无'}")
    delta_ok = bool(delta) and all(str(_get(d, "status")) == "pending" for d in delta)
    step("6d.portrait_delta 非空且全 pending", delta_ok,
         f"delta={json.dumps(delta, ensure_ascii=False)[:200]}")
    info(f"parent_msg 全文:\n{parent_msg}")

    # ── ⑦ 画像回读：出现新 pending 信号 ──
    r7 = await _get_student_profile(client, target_id)
    profile = r7.get("profile") or {}
    if isinstance(profile, str):
        profile = json.loads(profile)
    signals = profile.get("error_signals") or profile.get("errorSignals") or []
    new_pending = [s for s in signals
                   if str(_get(s, "status")) == "pending" and str(_get(s, "by")) == "system"]
    step("7.get_student_profile error_signals 出现新 pending 信号",
         bool(r7.get("ok") and new_pending),
         f"signals总数={len(signals)} pending(system)={len(new_pending)}")

    # ── ⑧ rebind-plan 换绑冒烟（R1a 简版真做：未上场次整体切新计划 → {rebound, unbound}）──
    r8p = await _upsert_course_plan(
        client,
        plan={"name": f"{plan_name}·换绑冒烟B", "target_type": "student", "target_id": target_id,
              "term_tag": "暑假", "year": 2026, "status": "1"},
        lessons=[{"lesson_seq": 1, "title": "换绑冒烟·课次1"},
                 {"lesson_seq": 2, "title": "换绑冒烟·课次2"}])
    plan_b = r8p.get("plan_id")
    step("8a.建换绑目标计划B(2课次)", bool(r8p.get("ok") and plan_b), f"plan_b={plan_b}")
    rb1 = await client.teacher_post(f"/teacher/schedule/target/0/{target_id}/rebind-plan",
                                    {"newPlanId": str(plan_b)})
    rb1 = rb1 if isinstance(rb1, dict) else {}
    step("8b.rebind-plan 返回 {rebound, unbound}",
         "rebound" in rb1 and "unbound" in rb1,
         f"rebound={rb1.get('rebound')} unbound={rb1.get('unbound')}")
    # 换绑回计划 A 复位（课次依 lesson_seq 顺配；第1场已上不在范围，lesson1 被其占用 → 余 12 课次接 12 场）
    rb2 = await client.teacher_post(f"/teacher/schedule/target/0/{target_id}/rebind-plan",
                                    {"newPlanId": str(plan_id)})
    rb2 = rb2 if isinstance(rb2, dict) else {}
    step("8c.换绑回计划A复位", "rebound" in rb2 and int(rb2.get("rebound") or 0) >= 1,
         f"rebound={rb2.get('rebound')} unbound={rb2.get('unbound')}")

    await cluster.aclose()
    print("\n===== G13 全链 PASS =====")
    print(f"target_id={target_id} plan_id={plan_id} pack_id={pack_id} first_session={first_session_id}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[FAIL] 未捕获异常: {type(e).__name__}: {e}")
        sys.exit(1)
    sys.exit(1 if _FAILED else 0)
