# -*- coding: utf-8 -*-
"""PRD-C-213 R7b · 备课 MCP 线路端到端终验脚本（agent 视角走完整备课链）。

模拟「老师说：帮我按计划备周思远的下节课」后，一个以『备课身份』连上 teacher-mcp 的
agent（=本脚本，扮演 opus）从零走到可打印材料 + 组卷的完整动作。全绿 = 备课线路验收通过。

调用姿势（照 g13_sujunyu.py）：绕开 MCP 协议、直接调工具实现——
  - schedule.py / qbank.py / lecture_read.py 有模块级 async 纯函数 `_xxx(client, ...)`，直接 import；
  - ingest.py / compose.py / kg.py / manual.py 的工具是 register() 闭包内定义，用一个「捕获式 shim」
    调 register(shim, client) 把闭包函数抓出来直接调（等价于真机调 MCP 工具，走同一份代码）。

数据铁律（本脚本遵守）：
  - 演示学生「周思远」+ 计划 + 场次 **保留不清理**（常驻演示样例）；幂等——重跑复用同一学生/计划/场次，不累加。
  - 变式/据讲义自造题一律入**私有池**（ingest_items → status='1' + is_public=0），**不 promote 不 set-public**（版权铁律）。
  - 私有题带常量标记 PREPE2EDEMO（题面尾），据此 keyword 断言 mine=True 捞回、mine=False（公共池）查不到。

跑法（需 C 线 :8090 + A 线 :8080 已起，同库 ai_lesson_prep@:3307）：
  cd D:\\workplace\\book-ai\\teacher-mcp
  .venv\\Scripts\\python.exe scripts\\prep_flow_e2e.py
"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # teacher-mcp 根

import pymysql

from app.config import settings
from app.ruoyi import RuoyiCluster, RuoyiError
from app.tools import compose as tool_compose
from app.tools import ingest as tool_ingest
from app.tools import kg as tool_kg
from app.tools import manual as tool_manual
from app.tools.ingest import IngestItem
from app.tools.lecture_read import _get_lecture_content, _list_lecture_docs
from app.tools.qbank import _get_question, _search_questions
from app.tools.schedule import (
    _build_prep_pack,
    _create_teach_target,
    _get_plan_detail,
    _get_student_profile,
    _list_teach_targets,
    _render_prep_pack,
    _schedule_sessions,
    _upsert_course_plan,
)

TODAY = dt.date.today()
MARKER = "PREPE2EDEMO"          # 私有题常量标记（题面尾，据此做私有 vs 公共断言；常量→重跑去重复用不累加）
STUDENT_NAME = "周思远"
PLAN_NAME = "周思远·2026暑期数学计划"
MATH_ROOT = "100"               # 七上数学教材根
UNIT_YLS = "100002"             # 第2章 有理数的运算（圈题锚点·子树）
LEAF_POWER = "100002005"        # 2.5 有理数的乘方（变式锚叶）
SCI_LECTURE_SUBJECT = "901001002001"  # 科学·课时1 长度的测量·体积的测量（讲义资产 book CC7S）
SCI_ROOT = "901"

# ───────────────────────── 断言小 harness ─────────────────────────
_LINES: list = []
_FAILED = False


def step(name: str, ok: bool, detail: str = "", hard: bool = True) -> bool:
    """记一步；hard=True 且 FAIL → 抛异常中断主链（summary 仍在 finally 落盘）。"""
    global _FAILED
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}" + (f" · {detail}" if detail else "")
    print(line)
    _LINES.append(line)
    if not ok:
        _FAILED = True
        if hard:
            raise AssertionError(name + (f" · {detail}" if detail else ""))
    return ok


def info(msg: str) -> None:
    print(f"       {msg}")
    _LINES.append(f"       {msg}")


# ───────────────────────── 捕获式 shim（把 register 闭包工具抓出来直接调）─────────────────────────
class _Shim:
    """假 mcp：@tool()/@resource() 只捕获函数、原样返回（闭包内互调不受影响）。"""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def resource(self, *a, **k):
        def deco(fn):
            return fn
        return deco


def _db():
    return pymysql.connect(host=settings.db_host, port=settings.db_port, user=settings.db_user,
                           password=settings.db_password, database=settings.db_database, charset="utf8mb4")


# ───────────────────────── 幂等 DB 查找（复用演示学生/计划/场次，不累加）─────────────────────────
def find_student(name: str, uid: int):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM biz_student WHERE name=%s AND create_by=%s ORDER BY id LIMIT 1", (name, uid))
            r = cur.fetchone()
            return str(r[0]) if r else None
    finally:
        conn.close()


def find_plan(target_id: str, name: str):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM biz_course_plan WHERE target_id=%s AND name=%s ORDER BY id LIMIT 1",
                        (int(target_id), name))
            r = cur.fetchone()
            return str(r[0]) if r else None
    finally:
        conn.close()


def existing_lessons(plan_id: str) -> dict:
    """{lesson_seq: lesson_id(str)}，用于重跑时把 id 回填进 lessons → 原地 update 不新增行。"""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lesson_seq, id FROM biz_course_plan_lesson WHERE plan_id=%s", (int(plan_id),))
            return {row[0]: str(row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def future_sessions(target_id: str) -> list:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, session_date FROM biz_schedule_session "
                        "WHERE target_id=%s AND target_type='0' AND session_date>=%s ORDER BY session_date",
                        (int(target_id), TODAY.isoformat()))
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        conn.close()


def q_status_public(qid: str):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, is_public FROM biz_question WHERE id=%s", (int(qid),))
            r = cur.fetchone()
            return (str(r[0]), int(r[1])) if r else (None, None)
    finally:
        conn.close()


# ───────────────────────── 演示数据（周思远富肖像 + 4 课次）─────────────────────────
PROFILE = {
    "traits": ["计算速度快但求稳不足", "空间想象好、爱动手", "遇到多步应用题容易急"],
    "level": {"desc": "五年级·班级中上", "target_layer": "巩固2层稳3层"},
    "env": "校内五年级 + 一对一提优",
    "history": [
        {"topic": "小数乘除", "status": "吃透", "src": "校内"},
        {"topic": "分数意义与运算", "status": "讲过未吃透", "src": "一对一"},
        {"topic": "简易方程", "status": "讲过未吃透", "src": "一对一·滚动复练"},
    ],
    "error_signals": [
        {"tag": "计算粗心", "evidence": "去括号变号漏、抄错数", "session_id": None,
         "ts": TODAY.isoformat(), "by": "teacher", "status": "confirmed"},
        {"tag": "多步应用审题跳步", "evidence": "复杂应用题条件读漏一句", "session_id": None,
         "ts": TODAY.isoformat(), "by": "teacher", "status": "confirmed"},
        {"tag": "负号/乘方符号", "evidence": "(-a)^n 的符号判断不稳", "session_id": None,
         "ts": TODAY.isoformat(), "by": "teacher", "status": "confirmed"},
    ],
}

DEFAULT_SEG_TEMPLATE = [
    {"name": "思维热身", "style": "开场·单点突破一题一坑", "topic": ""},
    {"name": "专项精练", "style": "分层挑题·由浅入深", "topic": ""},
    {"name": "课内过关", "style": "收尾过关·稳准", "topic": ""},
]


def _seg_tpl(topic: str) -> list:
    return [
        {"name": "思维热身", "style": "开场·单点突破一题一坑", "topic": ""},
        {"name": "专项精练", "style": "分层挑题·由浅入深", "topic": topic},
        {"name": "课内过关", "style": "收尾过关·稳准", "topic": topic},
    ]


def build_lessons(kg_ids: list) -> list:
    """4 课次，每课次带真实 kgNodeIds（七上数学真节点）+ 默认三段 segTemplate。"""
    return [
        {"lesson_seq": 1, "title": "有理数的乘方·符号规律", "lesson_type": "0",
         "source_ref": "演示·据七上数学 KG", "thinking_action": "从特殊到一般找符号规律",
         "layer_target": "2→3", "parent_copy": "带负号的连乘怎么定正负（乘方符号规律）",
         "kg_node_ids": kg_ids, "seg_template": _seg_tpl("有理数的乘方")},
        {"lesson_seq": 2, "title": "有理数混合运算·运算顺序", "lesson_type": "0",
         "source_ref": "演示·据七上数学 KG", "thinking_action": "分步 + 验算回查",
         "layer_target": "3", "parent_copy": "先算什么后算什么（混合运算顺序）",
         "kg_node_ids": kg_ids, "seg_template": _seg_tpl("有理数混合运算")},
        {"lesson_seq": 3, "title": "运算技巧·凑整与简算", "lesson_type": "0",
         "source_ref": "演示·据七上数学 KG", "thinking_action": "找结构·凑整简算",
         "layer_target": "3", "parent_copy": "一长串怎么算得又快又准（简便运算）",
         "kg_node_ids": kg_ids, "seg_template": _seg_tpl("运算技巧")},
        {"lesson_seq": 4, "title": "阶段小测", "lesson_type": "1",
         "source_ref": "演示·自选", "thinking_action": "", "layer_target": "",
         "parent_copy": "阶段小测：看看这段学得牢不牢",
         "kg_node_ids": kg_ids, "seg_template": DEFAULT_SEG_TEMPLATE},
    ]


# ───────────────────────── 变式 / 据讲义 自造题（agent 现场手写，含答案解析）─────────────────────────
def variant_items() -> list:
    """两道『有理数乘方』换情境变式（agent=opus 现场按圈到的真题改情境；题面尾带 MARKER 供私有断言）。"""
    return [
        IngestItem(
            stem=("一种细菌每 20 分钟分裂一次，一个分裂成两个。若培养皿中开始只有 1 个这种细菌，"
                  "在营养充足的条件下，经过 2 小时后培养皿中共有多少个细菌？（用乘方表示并算出结果）"
                  f"（备课线路演示题 {MARKER}）"),
            options=[], answer="64 个",
            analyze=("2 小时 = 120 分钟 = 6 个『20 分钟』周期，每过一个周期细菌数翻一倍，"
                     "故 6 个周期后为 $1\\times2^{6}=2^{6}=64$（个）。"),
            question_type=3, difficult=2, kp_id=LEAF_POWER,
            scenario="现实生活", free_tags=["乘方", "指数增长"],
            why="换情境到细菌分裂，把『连续翻倍』抽象成乘方，考查乘方的实际意义。"),
        IngestItem(
            stem=("计算：$\\left(-\\dfrac{1}{2}\\right)^{4}\\times(-2)^{3}$。"
                  f"（备课线路演示题 {MARKER}）"),
            options=[], answer="$-\\dfrac{1}{2}$",
            analyze=("先算乘方：$\\left(-\\dfrac{1}{2}\\right)^{4}=\\dfrac{1}{16}$（偶次幂为正），"
                     "$(-2)^{3}=-8$（奇次幂为负）；再相乘：$\\dfrac{1}{16}\\times(-8)=-\\dfrac{1}{2}$。"),
            question_type=7, difficult=2, kp_id=LEAF_POWER,
            scenario="纯数学", free_tags=["乘方", "符号判断"],
            why="在原换数变式上强化『(-a)^n 奇偶次幂定号』这一易错点。"),
    ]


def lecture_item() -> IngestItem:
    """据科学讲义『长度的测量』正文出的 1 道题（多次测量取平均减小误差）。锚科学 KG，入私有池。"""
    return IngestItem(
        stem=("小明用最小刻度为毫米的刻度尺测量科学课本的长度，四次测量的结果分别为 "
              "25.81cm、25.82cm、25.80cm、25.83cm。根据多次测量取平均值以减小误差的方法，"
              "该课本的长度应记为多少？"
              f"（备课线路演示题 {MARKER}）"),
        options=[], answer="25.82cm",
        analyze=("多次测量取平均值可减小偶然误差。"
                 "$(25.81+25.82+25.80+25.83)\\div4=103.26\\div4=25.815\\approx25.82$（cm），"
                 "结果保留到与测量值相同的位数（0.01cm）。"),
        question_type=7, difficult=2, kp_id=SCI_LECTURE_SUBJECT,
        scenario="科学跨学科", free_tags=["长度测量", "误差", "取平均"],
        why="据讲义『长度的测量·减小误差取平均』讲法出的同源计算题。")


# ───────────────────────── 主链 ─────────────────────────
async def main() -> None:
    cluster = RuoyiCluster()
    # shim 抓闭包工具（ingest/compose/kg/manual 都 register 到 cluster.a=A 线 :8080）
    ish, csh, ksh, msh = _Shim(), _Shim(), _Shim(), _Shim()
    tool_ingest.register(ish, cluster.a)
    tool_compose.register(csh, cluster.a)
    tool_kg.register(ksh, cluster.a)
    tool_manual.register(msh, cluster.a)
    ingest_items = ish.tools["ingest_items"]
    compose_paper = csh.tools["compose_paper"]
    list_kg_tree = ksh.tools["list_kg_tree"]
    resolve_kg = ksh.tools["resolve_kg"]
    get_role_manual = msh.tools["get_role_manual"]

    private_qids: list = []

    # ── 1. 登录 + 取备课手册 ──
    u, p = settings.ruoyi_username, settings.ruoyi_password
    await cluster.login(u, p)                 # 登 A 线 + 记凭据
    client_c = await cluster.ensure_c()       # 懒登录 C 线 :8090（备课线全走它）
    step("1a.login 双底座", bool(cluster.a.has_session() and client_c.has_session()),
         f"A.uid={cluster.a.user_id} C.uid={client_c.user_id}")
    man = get_role_manual(role="prep")
    mtext = man.get("manual") or ""
    step("1b.get_role_manual(role=prep) 取到备课手册",
         bool(man.get("ok") and man.get("role") == "prep" and "备课" in mtext and "私有池" in mtext),
         f"role={man.get('role')} len={len(mtext)}")

    # ── 2a. KG 锚点：list_kg_tree + resolve_kg 在七上数学树里找真实节点 ──
    tree = await list_kg_tree()
    step("2a1.list_kg_tree 取到平台知识点树", bool(tree.get("ok") and tree.get("nodes")),
         f"顶层节点={len(tree.get('nodes') or [])}")
    rk_unit = resolve_kg(subject_root=MATH_ROOT, query="有理数的运算")
    rk_leaf = resolve_kg(subject_root=MATH_ROOT, section_num="2.5")
    unit_hit = [n for n in rk_unit.get("nodes", []) if str(n.get("id")) == UNIT_YLS]
    leaf_hit = [n for n in rk_leaf.get("nodes", []) if str(n.get("id")) == LEAF_POWER]
    step("2a2.resolve_kg 命中真实单元/叶子节点",
         bool(unit_hit and leaf_hit),
         f"单元 {UNIT_YLS}={'有' if unit_hit else '无'} / 叶子 {LEAF_POWER}={'有' if leaf_hit else '无'}")
    kg_ids = [UNIT_YLS, LEAF_POWER]

    # ── 2b. 新建/复用演示学生周思远（富肖像）──
    uid = cluster.a.user_id
    target_id = find_student(STUDENT_NAME, uid)
    if target_id:
        info(f"演示学生「{STUDENT_NAME}」已存在 → 复用 target_id={target_id}（幂等·不新建）")
    else:
        r = await _create_teach_target(client_c, "student", STUDENT_NAME, grade_no=5, grade_year=2026,
                                       textbook_edition="2", subject="1", profile=PROFILE)
        target_id = r.get("id")
        info(f"新建演示学生「{STUDENT_NAME}」→ target_id={target_id}")
    step("2b.演示学生就绪(五年级/数学/人教/富肖像)", bool(target_id), f"target_id={target_id}")

    # ── 2c. 新建/复用课程计划 + 4 课次（真实 kgNodeIds + segTemplate）──
    plan_id = find_plan(target_id, PLAN_NAME)
    lessons = build_lessons(kg_ids)
    if plan_id:
        seqmap = existing_lessons(plan_id)
        for lz in lessons:
            if lz["lesson_seq"] in seqmap:
                lz["id"] = seqmap[lz["lesson_seq"]]     # 回填 id → 原地 update 不新增行
        info(f"计划已存在 → 复用 plan_id={plan_id}，回填 {len(seqmap)} 个课次 id 原地更新（幂等）")
    plan_body = {"name": PLAN_NAME, "target_type": "student", "target_id": target_id,
                 "term_tag": "暑假", "year": 2026, "material_note": "备课线路演示·据七上数学 KG 出题",
                 "default_seg_template": DEFAULT_SEG_TEMPLATE, "status": "1"}
    if plan_id:
        plan_body["id"] = plan_id
    r2 = await _upsert_course_plan(client_c, plan=plan_body, lessons=lessons)
    plan_id = r2.get("plan_id")
    lesson_ids = r2.get("lesson_ids", [])
    step("2c.upsert_course_plan 计划+4课次(带真实kgNodeIds+segTemplate)",
         bool(r2.get("ok") and plan_id and len(lesson_ids) == 4),
         f"plan_id={plan_id} lessons={len(lesson_ids)}")

    # ── 2d. 排 2 场未来课（幂等：已有未来场次则复用）──
    fut = future_sessions(target_id)
    if len(fut) >= 2:
        created = [{"id": sid, "sessionDate": d} for sid, d in fut]
        info(f"已存在 {len(fut)} 场未来场次 → 复用不重排（幂等）：{[d for _, d in fut][:4]}")
        step("2d.schedule_sessions 2 场未来课就绪", True, f"复用 created={len(created)}")
    else:
        d1 = (TODAY + dt.timedelta(days=(5 - TODAY.weekday()) % 7 or 7)).isoformat()  # 下个周六
        d2 = (dt.date.fromisoformat(d1) + dt.timedelta(days=7)).isoformat()
        items = [{"date": d1, "start": "09:30", "end": "11:00"},
                 {"date": d2, "start": "09:30", "end": "11:00"}]
        r3 = await _schedule_sessions(client_c, "student", target_id, items,
                                      plan_id=plan_id, auto_bind=True, force=False)
        created, conflicts = r3.get("created", []), r3.get("conflicts", [])
        if not created and conflicts:
            r3 = await _schedule_sessions(client_c, "student", target_id, items,
                                          plan_id=plan_id, auto_bind=True, force=True)
            created = r3.get("created", [])
        step("2d.schedule_sessions 排 2 场未来课", bool(r3.get("ok") and len(created) == 2),
             f"created={len(created)} dates={[d1, d2]}")

    # ── 3. 读肖像 + 读课次蓝本（agent 的备课输入）──
    r_prof = await _get_student_profile(client_c, target_id)
    prof = r_prof.get("profile") or {}
    if isinstance(prof, str):
        prof = json.loads(prof)
    traits = prof.get("traits") or []
    esig = prof.get("error_signals") or prof.get("errorSignals") or []
    step("3a.get_student_profile 肖像可读(traits/error_signals)",
         bool(r_prof.get("ok") and traits and esig),
         f"traits={len(traits)} error_signals={len(esig)}")
    r_pd = await _get_plan_detail(client_c, plan_id)
    plans_lessons = r_pd.get("lessons") or []
    l1 = plans_lessons[0] if plans_lessons else {}
    kg1 = l1.get("kgNodeIds") or l1.get("kg_node_ids") or []
    seg1 = l1.get("segTemplate") or l1.get("seg_template") or []
    if isinstance(kg1, str):
        kg1 = json.loads(kg1)
    if isinstance(seg1, str):
        seg1 = json.loads(seg1)
    step("3b.get_plan_detail 课次蓝本(kgNodeIds/segTemplate 非空)",
         bool(r_pd.get("ok") and kg1 and seg1),
         f"课次数={len(plans_lessons)} 课1.kgNodeIds={kg1} segTemplate段数={len(seg1)}")

    # ── 4. 按课次锚点圈题（子树召回）──
    r_sq = await _search_questions(client_c, subject_id=UNIT_YLS, page_size=30)
    real_items = r_sq.get("items") or []
    real_qids = [it["id"] for it in real_items if it.get("id")]
    step("4.search_questions 按锚点圈题(total>0)",
         bool(r_sq.get("ok") and r_sq.get("total", 0) > 0 and len(real_qids) >= 8),
         f"total={r_sq.get('total')} 取回 qids={len(real_qids)}")
    # 装段前看清一道母题题面（题为锚·先看后装）
    base_qid = real_qids[0]
    r_gq = await _get_question(client_c, [base_qid])
    base = (r_gq.get("items") or [{}])[0]
    info(f"母题 qid={base_qid} 题面: {(base.get('stem_text') or '')[:80]}…")

    # ── 5. 变式补题路径：自造 2 道变式 → 入私有池 → mine=True 捞回 / mine=False 查不到 ──
    rv = await ingest_items(items=variant_items(), subject_root=MATH_ROOT)
    v_qids = [str(x["question_id"]) for x in (rv.get("results") or []) if x.get("question_id")]
    private_qids += v_qids
    step("5a.ingest_items 2 变式入私有池",
         bool(rv.get("ok") and len(v_qids) == 2),
         f"new_qids={v_qids} stats={rv.get('stats')}")
    # 落库态断言：status='1' + is_public=0（私有已发布）
    stpub = [q_status_public(q) for q in v_qids]
    step("5b.变式落库态 status='1' + is_public=0(私有,不promote)",
         all(s == "1" and pub == 0 for s, pub in stpub), f"{stpub}")
    # mine=True 捞回命中
    r_mine = await _search_questions(client_c, keyword=MARKER, mine=True, page_size=30)
    mine_ids = {it["id"] for it in (r_mine.get("items") or [])}
    step("5c.search_questions(mine=True) 捞回自造变式",
         all(q in mine_ids for q in v_qids),
         f"mine捞回={len(mine_ids)} 含2变式={all(q in mine_ids for q in v_qids)}")
    # mine=False 公共池查不到（版权红线自动生效）
    r_pub = await _search_questions(client_c, keyword=MARKER, mine=False, page_size=30)
    pub_ids = {it["id"] for it in (r_pub.get("items") or [])}
    step("5d.search_questions(mine=False) 公共池查不到私有题",
         not any(q in pub_ids for q in v_qids),
         f"公共池命中MARKER={len(pub_ids)}(应=0)")

    # ── 6. 据讲义出题路径：读讲义正文 → 手写 1 道同源题入私有池 ──
    r_cat = await _list_lecture_docs(client_c, book_id="CC7S")
    r_lec = await _get_lecture_content(client_c, subject_id=SCI_LECTURE_SUBJECT, book_id="CC7S")
    ltext = r_lec.get("text") or ""
    step("6a.get_lecture_content 读讲义正文(text 非空)",
         bool(r_lec.get("ok") and r_lec.get("has_content") and len(ltext) > 50),
         f"catalog课时={len(r_cat.get('lessons') or [])} text长度={len(ltext)} 例题qid={len(r_lec.get('example_qids') or [])}")
    info(f"讲义正文首段: {ltext[:80].replace(chr(10), ' ')}…")
    rl = await ingest_items(items=[lecture_item()], subject_root=SCI_ROOT)
    l_qids = [str(x["question_id"]) for x in (rl.get("results") or []) if x.get("question_id")]
    private_qids += l_qids
    step("6b.据讲义出题 1 道入私有池",
         bool(rl.get("ok") and len(l_qids) == 1),
         f"new_qid={l_qids}")
    # 确认据讲义题也在私有池、可 get 回
    if l_qids:
        s, pub = q_status_public(l_qids[0])
        r_lget = await _get_question(client_c, l_qids)
        got = bool((r_lget.get("items") or []))
        step("6c.据讲义题私有(status1/pub0)且可读回", (s == "1" and pub == 0 and got),
             f"status={s} is_public={pub} get回={got}")

    # ── 7. 装包三段(段2 groups 混装真题+私有新题) → 渲染单文件 pages>=3 ──
    seg_qids_1 = real_qids[0:2]
    seg_qids_3 = real_qids[2:6]
    grp_real = real_qids[6:9]
    segs = [
        {"name": "思维热身", "style": "开场·单点突破", "question_ids": seg_qids_1,
         "rules": "", "note": "2 道快速进入状态，一题一坑"},
        {"name": "专项精练·乘方符号", "style": "分层挑题·由浅入深",
         "groups": [{"title": "真题巩固", "question_ids": grp_real},
                    {"title": "变式提升(自造)", "question_ids": v_qids}],
         "rules": "真题巩固 + 自造变式提升", "note": "口诀：负数偶次幂为正、奇次幂为负；先定号再算值。"},
        {"name": "课内过关", "style": "收尾过关·稳准", "question_ids": seg_qids_3,
         "rules": "", "note": "稳准收尾，易错向"},
    ]
    r_bp = await _build_prep_pack(client_c, lesson_id=lesson_ids[0], segs=segs)
    pack_id = r_bp.get("pack_id")
    step("7a.build_prep_pack 三段(段2 groups 混装真题+私有变式)",
         bool(r_bp.get("ok") and pack_id), f"pack_id={pack_id}")
    r_rp = await _render_prep_pack(client_c, pack_id, mark_ready=True)
    arts = r_rp.get("artifacts") or []
    pages = 0
    if arts:
        a0 = arts[0]
        pages = a0.get("pages") if isinstance(a0, dict) else 0
    step("7b.render_prep_pack 单文件 artifact 且 pages>=3",
         bool(r_rp.get("ok") and len(arts) == 1 and (pages or 0) >= 3),
         f"artifacts={len(arts)} pages={pages}")
    for a in arts:
        if isinstance(a, dict):
            info(f"artifact: seg={a.get('seg')} file={a.get('file')} pages={a.get('pages')}")

    # ── 8. 组卷冒烟（非阻塞：失败仅记录不断链）──
    try:
        from app.tools.compose import OutlineItem
        outline = [OutlineItem(subjectId=LEAF_POWER, subjectName="有理数的乘方",
                               questionType=7, difficult=2, count=3),
                   OutlineItem(subjectId=UNIT_YLS, subjectName="有理数的运算",
                               questionType=1, difficult=2, count=3)]
        r_cp = await compose_paper(outline=outline, title="备课线路演示·有理数运算小卷")
        step("8.compose_paper 组卷冒烟(落库返回 paperId)",
             bool(r_cp.get("ok") and r_cp.get("paper_id")),
             f"paper_id={r_cp.get('paper_id')} item_count={r_cp.get('item_count')} reason={r_cp.get('reason')}",
             hard=False)
    except Exception as e:
        step("8.compose_paper 组卷冒烟", False, f"异常(非阻塞): {type(e).__name__}: {e}", hard=False)

    await cluster.aclose()

    # ── 9. 交付摘要 ──
    _LINES.append("")
    _LINES.append("===== 交付摘要 =====")
    _LINES.append(f"演示学生 周思远 target_id={target_id}（保留·常驻演示样例）")
    _LINES.append(f"课程计划 plan_id={plan_id} · 课次 lesson_ids={lesson_ids}")
    _LINES.append(f"备课包 pack_id={pack_id} · 渲染产物 pages={pages}")
    _LINES.append(f"私有题 qids（is_public=0·不 promote·不 set-public）={private_qids}")
    _LINES.append(f"  - 变式2(乘方换情境/换数)={v_qids}")
    _LINES.append(f"  - 据讲义1(长度测量取平均)={l_qids}")
    for ln in _LINES[-7:]:
        print(ln)


def _write_summary() -> None:
    out = Path(r"d:\workplace\book-ai\workplace\.prd_ccw\PRD-C\PRD-C-213\over\R7-备课线路端到端输出.txt")
    header = [
        "PRD-C-213 R7b · 备课 MCP 线路端到端终验输出",
        f"跑机时间: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"总判定: {'FAIL' if _FAILED else 'PASS(全绿)'}",
        "=" * 60, "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header + _LINES) + "\n", encoding="utf-8")
    print(f"\n[summary] 已写: {out}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"[ABORT] 主链断言失败: {e}")
    except Exception as e:
        _FAILED = True
        _LINES.append(f"[FAIL] 未捕获异常: {type(e).__name__}: {e}")
        print(f"[FAIL] 未捕获异常: {type(e).__name__}: {e}")
    finally:
        _write_summary()
    sys.exit(1 if _FAILED else 0)
