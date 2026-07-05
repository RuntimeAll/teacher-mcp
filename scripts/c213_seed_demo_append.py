# -*- coding: utf-8 -*-
"""PRD-C-213 演示数据【追加】灌数（2026-07-06 指令）。

🔴 与 c213_seed_demo.py 的区别 = **纯追加、零清库、零裸 SQL 写业务表**：
  - 不 TRUNCATE / DELETE 任何表；既有苏俊宇两套档案 + 空计划全部原样保留。
  - 所有写入走 C 线 :8090 既有接口（schedule.py 纯函数 + class-students / parent-export 端点）。
  - 题目一律取【真实公开题 qid】（POST /teacher/question/page），不编造、不裸插 biz_question。

造什么（贴近真实老师使用场景）：
  ① 5 名学生（三/四/五/六/初一，新字段 gradeNo/gradeYear=2026/textbookEdition/subject 码；2 人富肖像）
     + 1 个班课（四年级奥数班，挂 3 名成员）。
  ② 每对象一份暑期课程计划（6 课次，真实标题：定义新运算/鸡兔同笼/最值问题/巧算周长…）
     + 批量排课未来 3 周（不同曜日/时段错开，周循环，2 节/周×3 周=6 场，1:1 绑课次）。
  ③ 状态多样性：1 场已上+回收（逐题对错→肖像 delta）、1 场请假（触发顺延）、1 场取消、
     2 条外部占位（信奥集训营 / 校内期末考试）。
  ④ 备课材料样例：2 个课次装真题→ pack A（含 groups 段内分组）render 单文件 PDF 标「已备好」；
     pack B 停在「装配中」（只 build 不 render）。
  ⑤ 家长版课表图 1 份（parent-export 冒烟）。

跑法（前置 :8090 已起）：teacher-mcp 下 `.venv\\Scripts\\python.exe scripts\\c213_seed_demo_append.py`
"""
import asyncio
import datetime as dt
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # teacher-mcp 根

from app.config import settings                       # noqa: E402
from app.ruoyi import RuoyiCluster, RuoyiError        # noqa: E402
from app.tools import schedule as S                   # noqa: E402

# ── 曜日/时段（不与既有苏俊宇的 周二09:30 / 周日13:30 撞，也彼此不撞）──
AM1, AM2 = ("09:00", "10:30"), ("10:40", "12:10")
PM1, PM2 = ("14:00", "15:30"), ("15:50", "17:20")
EVE = ("20:00", "21:00")
WEEK_START, WEEKS = dt.date(2026, 7, 6), 3            # 周一起，3 周：7/6 ~ 7/25

# 富肖像（2 人）
PROFILE_WANG = {
    "traits": ["注意力短，需游戏化引导", "口头表达强、书写慢"],
    "level": {"desc": "启蒙段，校内中上，兴趣优先", "target_layer": "1→2层"},
    "env": "无外部竞赛班，家中有陪读",
    "history": [
        {"topic": "20 以内进退位", "status": "吃透", "src": "校内"},
        {"topic": "图形找规律", "status": "讲过未吃透", "src": "暑期试听课"},
    ],
    "error_signals": [
        {"tag": "抄错数", "evidence": "题目数字抄错致算错", "session_id": None,
         "ts": "2026-07-06", "by": "teacher", "status": "confirmed"},
        {"tag": "审题跳字", "evidence": "读题漏条件", "session_id": None,
         "ts": "2026-07-06", "by": "teacher", "status": "confirmed"},
    ],
}
PROFILE_CHEN = {
    "traits": ["逻辑强、要求高", "考试易紧张"],
    "level": {"desc": "校内优秀，备战小升初民办", "target_layer": "3→4层"},
    "env": "外部一家机构大班课同步上",
    "history": [
        {"topic": "分数四则", "status": "吃透", "src": "校内"},
        {"topic": "行程·相遇追及", "status": "讲过未吃透", "src": "外部大班课"},
        {"topic": "工程问题", "status": "讲过未吃透", "src": "外部大班课"},
    ],
    "error_signals": [
        {"tag": "单位换算", "evidence": "面积/体积单位换算易错", "session_id": None,
         "ts": "2026-07-06", "by": "teacher", "status": "confirmed"},
        {"tag": "考试节奏", "evidence": "压轴题耗时过久致前面失分", "session_id": None,
         "ts": "2026-07-06", "by": "teacher", "status": "confirmed"},
    ],
}


def _lessons(titles):
    """标题列表 → 课次 dict（末位设为测试课，其余教学课）。"""
    out = []
    for i, t in enumerate(titles, 1):
        lt = "1" if i == len(titles) else "0"
        out.append({"lesson_seq": i, "title": t, "lesson_type": lt,
                    "source_ref": "暑期自编大纲", "layer_target": "2→3"})
    return out


# 6 名对象（5 学生 + 1 班课）——(kind, name, grade_no, edition, subject, profile, weekly[(wd,slot)], plan_name, titles)
OBJECTS = [
    ("student", "王雨桐", 3, "2", "1", PROFILE_WANG, [(0, AM1), (3, PM2)],
     "王雨桐·2026暑期趣味数学",
     ["巧算与速算", "有趣的图形认识", "找规律", "简单推理", "时间与人民币", "趣味应用·结业测"]),
    ("student", "韩梓萱", 4, "2", "1",
     {"traits": ["计算快、爱抢答"], "level": {"desc": "校内良好，冲奥数入门", "target_layer": "2→3层"},
      "env": "", "history": [], "error_signals": []},
     [(0, PM1), (2, EVE)],
     "韩梓萱·2026暑期奥数入门",
     ["定义新运算", "鸡兔同笼", "和差倍问题", "周期问题", "最值问题", "巧算周长与面积·结业测"]),
    ("student", "林悦然", 5, "2", "1",
     {"traits": ["细心但速度偏慢"], "level": {"desc": "校内扎实，奥数强化", "target_layer": "3层"},
      "env": "外部奥数班同步", "history": [], "error_signals": []},
     [(1, PM1), (4, AM1)],
     "林悦然·2026暑期奥数强化",
     ["分数巧算", "行程问题", "工程问题", "数论入门", "几何面积", "逻辑推理·结业测"]),
    ("student", "陈子墨", 6, "2", "1", PROFILE_CHEN, [(1, PM2), (5, AM2)],
     "陈子墨·2026小升初冲刺",
     ["比与比例", "百分数应用", "圆的周长与面积", "列方程解应用", "立体图形", "综合突破·结业测"]),
    ("student", "沈亦辰", 7, "1", "2",
     {"traits": ["动手能力强、爱提问"], "level": {"desc": "校内中上，实验题突出", "target_layer": "3层"},
      "env": "", "history": [], "error_signals": []},
     [(2, AM1), (6, AM2)],
     "沈亦辰·2026暑期科学",
     ["显微镜与细胞", "物质的密度", "机械运动", "声与光", "物态变化", "科学探究方法·结业测"]),
    ("class", "四年级奥数班", 4, "2", "1",
     {"traits": ["小班 3-6 人", "整体基础中上"], "level": {"desc": "四年级奥数班课，进度统一", "target_layer": "3层"},
      "env": "线下小班", "history": [], "error_signals": []},
     [(5, PM1), (6, PM2)],
     "四年级奥数班·2026暑期",
     ["定义新运算", "鸡兔同笼", "最值问题", "巧算周长", "数阵图", "还原问题·结业测"]),
]

OK = True


def step(name, ok, detail=""):
    global OK
    print(("[PASS] " if ok else "[FAIL] ") + name + (" → " + str(detail) if detail else ""))
    if not ok:
        OK = False


def _get(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _sid(s):
    return str(_get(s, "id", "sessionId") or "")


def _sorted(created):
    return sorted([c for c in created if isinstance(c, dict)],
                  key=lambda x: (str(_get(x, "sessionDate", "date") or ""),
                                 str(_get(x, "startTime", "start") or "")))


async def _fetch_qids(client, need):
    ids, page = [], 1
    while len(ids) < need and page <= 10:
        resp = await client.teacher_post("/teacher/question/page", {"pageIndex": page, "pageSize": 50})
        rows = (resp or {}).get("list") or []
        if not rows:
            break
        ids += [str(_get(r, "id", "questionId")) for r in rows if _get(r, "id", "questionId")]
        page += 1
    return ids[:need]


async def main():
    cluster = RuoyiCluster()
    client = cluster.c
    try:
        await client.login(settings.ruoyi_username, settings.ruoyi_password)
    except Exception as e:
        step("登录 C 线 :8090", False, f"{type(e).__name__}: {e}（:8090 没起？）")
        sys.exit(1)
    step("登录 C 线 :8090", True, f"user={client.username}")

    qids = await _fetch_qids(client, 40)
    step("取 40 个真实公开题 qid", len(qids) >= 34, f"got={len(qids)}")

    created_map = {}   # plan_name -> {tid, kind, plan_id, lesson_ids, sessions[]}
    student_ids = []   # 供班课挂成员

    # ── ①②：建对象 + 计划 + 排课 ──
    for kind, name, gno, edition, subj, profile, weekly, plan_name, titles in OBJECTS:
        rt = await S._create_teach_target(client, kind, name, grade_no=gno, grade_year=2026,
                                          textbook_edition=edition, subject=subj,
                                          parent_phone="", profile=profile, color="")
        tid = str(rt.get("id"))
        step(f"①建档 {name}（{kind}·{gno}年级）", bool(tid) and tid != "None", f"id={tid}")
        if kind == "student":
            student_ids.append(tid)

        rp = await S._upsert_course_plan(
            client,
            {"name": plan_name, "target_type": kind, "target_id": tid,
             "term_tag": "暑假", "year": 2026, "material_note": "暑期自编·分层挑题",
             "status": "1"},
            _lessons(titles))
        plan_id = str(rp.get("plan_id"))
        lesson_ids = [str(x) for x in (rp.get("lesson_ids") or [])]
        step(f"②{name} 计划+{len(titles)}课次", len(lesson_ids) == len(titles), f"plan={plan_id}")

        items = []
        for w in range(WEEKS):
            for wd, (st, en) in weekly:
                d = WEEK_START + dt.timedelta(days=7 * w + wd)
                items.append({"date": d.isoformat(), "start": st, "end": en})
        rs = await S._schedule_sessions(client, kind, tid, items, plan_id=plan_id,
                                        auto_bind=True, force=True)
        sess = _sorted(rs.get("created") or [])
        step(f"②{name} 排课 {len(weekly)}节×{WEEKS}周", len(sess) == len(items),
             f"created={len(sess)}")
        created_map[plan_name] = {"tid": tid, "kind": kind, "plan_id": plan_id,
                                  "lesson_ids": lesson_ids, "sessions": sess}

    # ── ①b：班课挂 3 名成员（前三名学生）──
    cls = created_map["四年级奥数班·2026暑期"]
    member_ids = student_ids[:3]
    rcm = await client.teacher_post(f"/teacher/schedule/class/{cls['tid']}/students",
                                    {"studentIds": [int(x) for x in member_ids]})
    # 回读班课学员数
    tg = await S._list_teach_targets(client, target_type="class", keyword="四年级奥数班")
    cnt = None
    for it in tg.get("items", []):
        if str(_get(it, "id")) == cls["tid"]:
            cnt = _get(it, "studentCount", "memberCount", "studentNum")
    step("①b 班课挂 3 名成员", True, f"studentIds={member_ids} studentCount={cnt}")

    # ── ③ 状态多样性 ──
    # (a) 已上+回收：王雨桐第 1 场逐题对错 → 家长消息 + 肖像 delta
    wy = created_map["王雨桐·2026暑期趣味数学"]
    s0 = _sid(wy["sessions"][0])
    item_results = [
        {"question_id": qids[0], "seg": "第1段", "seq": 1, "result": "对", "cause": ""},
        {"question_id": qids[1], "seg": "第1段", "seq": 2, "result": "错", "cause": "计算"},
        {"question_id": qids[2], "seg": "第2段", "seq": 3, "result": "卡", "cause": "策略"},
        {"question_id": qids[3], "seg": "第2段", "seq": 4, "result": "对", "cause": ""},
    ]
    rv = await S._submit_review(client, s0, item_results,
                               teacher_note="首课状态不错，抄数问题仍需盯；简单题都拿下")
    pm = rv.get("parent_msg") or ""
    step("③a 王雨桐第1场 已上+回收（家长消息+肖像delta）",
         bool(pm.startswith("家长您好")), pm[:32].replace("\n", "/"))

    # (b) 请假顺延：韩梓萱第 2 场请假 → 触发后续绑定课次前移
    hx = created_map["韩梓萱·2026暑期奥数入门"]
    rl = await S._update_session(client, _sid(hx["sessions"][1]), "leave")
    step("③b 韩梓萱第2场 请假（触发顺延）", True,
         f"deferred={len(rl.get('deferred') or [])} overflow={len(rl.get('overflow') or [])}")

    # (c) 取消：林悦然第 3 场取消
    ly = created_map["林悦然·2026暑期奥数强化"]
    rc = await S._update_session(client, _sid(ly["sessions"][2]), "cancel")
    step("③c 林悦然第3场 取消", True, f"deferred={len(rc.get('deferred') or [])}")

    # (d) 2 条外部占位：挂沈亦辰名下（周四晨间，避开其排课）
    sy = created_map["沈亦辰·2026暑期科学"]
    ext = [
        {"date": "2026-07-09", "start": "08:00", "end": "09:30",
         "session_type": "3", "external_title": "信奥集训营·晨训", "note": "外部集训占位"},
        {"date": "2026-07-16", "start": "08:00", "end": "11:00",
         "session_type": "3", "external_title": "校内期末考试", "note": "学校统考占位"},
    ]
    re = await S._schedule_sessions(client, "student", sy["tid"], ext,
                                    auto_bind=False, force=True)
    step("③d 2 条外部占位（信奥集训营/校内期末考试）",
         len(re.get("created") or []) == 2, f"created={len(re.get('created') or [])}")

    # ── ④ 备课材料样例：pack A（含 groups）render 已备好 / pack B 停装配中 ──
    # pack A = 班课第 1 课次「定义新运算」，含段内分组
    segs_a = [
        {"name": "开场·思维热身", "style": "一题一坑", "question_ids": qids[4:6],
         "rules": "", "note": "2 道快速进入状态"},
        {"name": "定义新运算·专项", "style": "书挑题·分层",
         "groups": [{"title": "基础掌握", "question_ids": qids[6:11]},
                    {"title": "进阶挑战", "question_ids": qids[11:14]}],
         "rules": "基础5+进阶3", "note": "先套定义、再代入；注意运算顺序不同于常规四则"},
        {"name": "课内同步·收尾", "style": "简单过关", "question_ids": qids[14:18],
         "rules": "", "note": ""},
    ]
    rba = await S._build_prep_pack(client, lesson_id=cls["lesson_ids"][0], segs=segs_a)
    pack_a = str(rba.get("pack_id"))
    rra = await S._render_prep_pack(client, pack_a, mark_ready=True)
    arts = rra.get("artifacts") or []
    step("④a pack A（班课·定义新运算·含groups）render 单文件·已备好",
         bool(pack_a and pack_a != "None") and len(arts) == 1
         and (_get(arts[0], "pages") or 0) >= 3,
         f"pack={pack_a} pages={[_get(a, 'pages') for a in arts]}")

    # pack B = 陈子墨第 1 课次「比与比例」，只 build 不 render → 停「装配中」
    cz = created_map["陈子墨·2026小升初冲刺"]
    segs_b = [
        {"name": "开场·思维热身", "style": "一题一坑", "question_ids": qids[18:20],
         "rules": "", "note": ""},
        {"name": "比与比例·专项", "style": "书挑题", "question_ids": qids[20:28],
         "rules": "8 题", "note": "化简比→求比值→按比分配三步走"},
        {"name": "课内同步", "style": "简单过关", "question_ids": qids[28:32],
         "rules": "", "note": ""},
    ]
    rbb = await S._build_prep_pack(client, lesson_id=cz["lesson_ids"][0], segs=segs_b)
    pack_b = str(rbb.get("pack_id"))
    step("④b pack B（陈子墨·比与比例）build 后停「装配中」（不 render）",
         bool(pack_b and pack_b != "None"), f"pack={pack_b}")

    # ── ⑤ 家长版课表图 1 份（parent-export 冒烟）：王雨桐计划 ──
    rpe = await client.teacher_post(
        f"/teacher/schedule/plan/{wy['plan_id']}/parent-export?targetId={wy['tid']}", {})
    pf = (rpe or {}).get("file")
    pu = (rpe or {}).get("url")
    step("⑤ parent-export 家长版课表图返回文件", bool(pf or pu), f"file={pf}")

    await cluster.aclose()
    print("\n===== 追加演示数据 " + ("完成" if OK else "存在 FAIL") + " =====")
    print("对象→计划→场次：")
    for pn, v in created_map.items():
        print(f"  {v['kind']:7} plan={v['plan_id']} tid={v['tid']} 场次={len(v['sessions'])}  {pn}")
    print(f"pack_A(已备好)={pack_a}  pack_B(装配中)={pack_b}")
    print(f"parent_export_file={pf}")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[FAIL] 未捕获异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
