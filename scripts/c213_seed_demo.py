# -*- coding: utf-8 -*-
"""PRD-C-213 演示数据重造（维护者 2026-07-05 指令）：
① 清空教学安排域 8 表（dev）+ 复位公开题上的测试星级标注；
② 苏俊宇第 1 次课三卷真题 31 道（09a/09b/09c 正本）入**私有题池**（is_public=0，带星级/专项/素材源）；
③ 苏俊宇全套原样重建（13 次暑期计划不动 + 13 场 + 第 1 次课备课包用真题 render + 回收）；
④ 新造 5 名学生，周循环排课 4 周（2026-07-06 ~ 08-02）：每天 3-4 节，上午两节(09:00-10:30/10:40-12:10)、
   下午两节(14:00-15:30/15:50-17:20)。
跑法：teacher-mcp 下 `.venv\\Scripts\\python.exe scripts\\c213_seed_demo.py`（前置 :8090 已起）。
"""
import asyncio
import datetime as dt
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pymysql  # noqa: E402

from app.tools import schedule as S  # noqa: E402
from app.ruoyi import RuoyiCluster  # noqa: E402
from app.config import settings  # noqa: E402
import g13_sujunyu as G  # noqa: E402  # 复用 PROFILE/LESSONS/SESSION_SLOTS（import 安全：有 __main__ 守卫）

DB = dict(host="127.0.0.1", port=3307, user="root", password="123456",
          database="ai_lesson_prep", charset="utf8mb4")

# ── ② 三卷真题（09a/09b/09c 正本逐字抄，31 道）──
# (stem, qtype 1选/4填/5解答, star_level, topic_tag, source_ref, subject_id)
Q_MIND = [
    ("一百只鸡可以换一只羊，五十只羊可以换一头牛，那么十万只鸡可以换多少头牛？", 5, None),
    ("把一个棱长为 1 米的正方体切割成棱长是 1 厘米的小正方体，然后将全部的小正方体排成一行，这一行有多长？先猜一猜，再算一算。", 5, None),
]
Q_ZX = [
    ("已知两个非零自然数的和是 39，这两个自然数的积最大是多少？最小是多少？", 5, "1"),
    ("李大爷要用长 30 米的篱笆围成一个长方形养鸡场，围得的养鸡场的面积最大是多少平方米？（长和宽均取整米数）", 5, "1"),
    ("用 2、3、4、5 这四个数组成两个两位数，这两个数的乘积最大是多少？", 5, "1"),
    ("用 2、3、4、5 这四个数组成两个两位数，这两个数的乘积最小是多少？", 5, "1"),
    ("有一类数，它的每个数位上的数字之和是 40，这类数中最小的数是多少？", 5, "1"),
    ("有一类数，它们的各数位上的数字之和都是 37，在这类数中，最小的数是几？", 5, "1"),
    ("在六位数 865473 的某一位数字后面插入一个同样的数字，可以得到一个七位数。这个七位数最小是多少？最大是多少？", 5, "1"),
    ("用长 36 米的篱笆靠墙围成一块长方形菜地（长、宽均取整米数），这块长方形菜地的面积最大是多少？", 5, "2"),
    ("用若干根长为 2 厘米的小棒围成一个周长为 100 厘米的长方形，这个长方形的面积最大是多少平方厘米？", 5, "2"),
    ("三个自然数的乘积是 180，这三个自然数之和最小是多少？", 5, "2"),
    ("六个互不相等的正整数之和是 45，将这六个数从小到大排列，第 5 个数最大是多少？", 5, "2"),
    ("用 1、3、5、7、8、9 这六个数字分别组成两个三位数，使这两个三位数的积最大。积最大是多少？", 5, "2"),
    ("把 1、2、3、4、5、6、7、8 填入算式 □□□□−□□×□□，使得数最大，这个最大得数是多少？", 5, "2"),
    ("一个三位数除以 43，商是 a，余数是 b（a、b 都是整数），求 a+b 的最大值。", 5, "2"),
    ("a、b 是 1，2，3，…，99，100 中的两个不同的数，求 (a+b)÷(a−b) 的最大值。", 5, "2"),
    ("把 16 拆分成几个自然数的和，这几个自然数的积最大是多少？", 5, "3"),
    ("用 1～9 这九个数字分别组成三个不同的三位数，使这三个三位数的积最小。", 5, "3"),
    ("四个互不相同的自然数的积是 546，这四个自然数的和最大是多少？", 5, "3"),
    ("有一个电子表用 5 个两位数来表示时间，如 14:32:45/08/28 表示 8 月 28 日 14 时 32 分 45 秒。当电子表上显示的 10 个数字都不同时，这 5 个两位数的和最大是多少？", 5, "3"),
    ("已知 12345678910111213…282930 是一个多位数，从中画去 40 个数字，使剩下的数字（顺序不变）组成一个多位数，这个多位数最大是多少？最小是多少？", 5, "3"),
]
Q_KN = [
    ("读出下面各数。<br>60606000 读作 ______<br>500070008 读作 ______", 4, None),
    ("写出下面各数。<br>四亿零四十万零四 写作 ______　　一千零一十万零一百 写作 ______", 4, None),
    ("一个数由 8 个亿、40 个万和 5 个一组成，这个数写作 ______。", 4, None),
    ("用 5、0、0、0、8 这五个数字组成五位数：只读出一个“零”的最大数是 ______，一个“零”都不读的最大数是 ______。", 4, None),
    ("一个六位数，省略“万”后面的尾数后约是 30 万。这个数最大是 ______，最小是 ______。", 4, None),
    ("在〇里填 &gt;、&lt; 或 =。<br>4600 万 〇 45999999　　　　10 个十万 〇 1000 个百", 4, None),
    ("一个八位数，若将最高位上的 7 写成 1，十万位上的 0 写成 6，得数比原数小了多少？", 5, None),
    ("一个五位数，若在它的左端写上数字 9，得数是原数的 16 倍，原数是多少？", 5, None),
    ("用数字卡片 0～9 排一个六位数：前两位的数字和是 1，中间两位的数字积是 25，最后两位的数字和为 18。这个六位数是多少？", 5, None),
]

# ── ④ 5 名新学生 + 周循环槽位（AM1/AM2/PM1/PM2；weekday: 0=周一…6=周日）──
AM1, AM2 = ("09:00", "10:30"), ("10:40", "12:10")
PM1, PM2 = ("14:00", "15:30"), ("15:50", "17:20")
STUDENTS = [
    # R1a 建模：(name, 标签, grade_no, grade_year, textbook_edition码, subject码,
    #            traits, level_desc, target_layer, weekly[(weekday,slot)])
    # 暑期录「升X」= gradeNo 升入年级 + gradeYear 2026；edition：1浙教/2人教；subject：1数学/2科学
    ("林悦然", "升六·数学", 6, 2026, "2", "1", ["细心但速度偏慢"], "校内扎实，备战小升初", "3层",
     [(0, AM1), (1, PM2), (3, AM1), (6, AM2)]),
    ("陈子墨", "初一·科学", 7, 2026, "1", "2", ["动手能力强，爱提问"], "校内中上，实验题突出", "3层",
     [(0, PM1), (2, PM2), (3, AM2), (5, AM1)]),
    ("王雨桐", "升三·数学", 3, 2026, "2", "1", ["注意力短，需游戏化引导"], "启蒙段，兴趣优先", "2层",
     [(0, AM2), (2, AM1), (3, PM1), (4, PM2)]),
    ("赵一鸣", "初二·数学", 8, 2026, "2", "1", ["基础有漏洞，畏难"], "校内中游，先补基本功", "2→3",
     [(2, AM2), (4, PM1), (5, PM1), (6, AM1)]),
    ("韩笑笑", "升五·数学", 5, 2026, "2", "1", ["口算快，书写乱"], "校内良好，冲奥数入门", "3层",
     [(1, PM1), (2, PM1), (4, AM1), (5, AM2)]),
]
WEEK_START, WEEKS = dt.date(2026, 7, 6), 4  # 周一起，4 周：7/6 ~ 8/2

OK = True
def step(name, ok, detail=""):
    global OK
    print(("[PASS] " if ok else "[FAIL] ") + name + (" → " + str(detail) if detail else ""))
    if not ok:
        OK = False


def wipe_and_ingest():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    for t in ["biz_session_review", "biz_prep_pack", "biz_schedule_session",
              "biz_course_plan_lesson", "biz_course_plan", "biz_class_student",
              "biz_class", "biz_student"]:
        cur.execute(f"TRUNCATE TABLE {t}")
    step("①清空教学安排域 8 表", True)
    cur.execute("UPDATE biz_question SET star_level=NULL, topic_tag=NULL, source_ref=NULL "
                "WHERE is_public=1 AND (star_level IS NOT NULL OR topic_tag IS NOT NULL OR source_ref IS NOT NULL)")
    step("①复位公开题测试标注", True, f"rows={cur.rowcount}")
    # 幂等：清掉本脚本此前灌过的私有池题（只删自己造的：私有+admin+本卡三个专项名+自编来源）
    cur.execute("DELETE FROM biz_question WHERE is_public=0 AND create_by='1' AND source_type=6 "
                "AND topic_tag IN ('思维题','最值','大数的认识')")
    step("①清理旧私有池种子", True, f"rows={cur.rowcount}")

    ids = {"mind": [], "zx": [], "kn": []}
    def ins(stem, qtype, star, topic, src, subj):
        cur.execute(
            "INSERT INTO biz_question(question_type, subject_id, stem_text, status, is_public,"
            " create_by, create_user, star_level, topic_tag, source_ref, source_type, create_time)"
            " VALUES (%s,%s,%s,'1',0,'1',1,%s,%s,%s,6,NOW())",
            (qtype, subj, stem, star, topic, src))
        return str(cur.lastrowid)
    for stem, qt, star in Q_MIND:
        ids["mind"].append(ins(stem, qt, star, "思维题", "自制·趣味巧思", "307"))
    for stem, qt, star in Q_ZX:
        ids["zx"].append(ins(stem, qt, star, "最值", "学而思36周书·第11周+进阶材料", "307"))
    for stem, qt, star in Q_KN:
        ids["kn"].append(ins(stem, qt, star, "大数的认识", "自制·课内过关", "307001"))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM biz_question WHERE is_public=0 AND create_by='1' AND topic_tag IS NOT NULL")
    n = cur.fetchone()[0]
    step("②三卷真题 31 道入私有题池", n == 31, f"in_db={n} (思2/专项20/课内9)")
    conn.close()
    return ids


async def main():
    ids = wipe_and_ingest()

    cluster = RuoyiCluster()
    client = cluster.c
    await client.login(settings.ruoyi_username, settings.ruoyi_password)
    step("登录 C 线 :8090", True)

    # ── ③ 苏俊宇全套（原计划不动：PROFILE/LESSONS/SESSION_SLOTS 逐字复用 g13 正本）──
    # R1a 建模：升四 = gradeNo 4 + gradeYear 2026；人教='2'、数学='1'
    r = await S._create_teach_target(client, "student", "苏俊宇", grade_no=4, grade_year=2026,
                                     textbook_edition="2", subject="1", parent_phone="13800002211",
                                     profile=G.PROFILE, color="")
    su_id = str(r.get("id"))
    step("③建档 苏俊宇（完整肖像·新字段）", bool(su_id), f"id={su_id}")

    rp = await S._upsert_course_plan(
        client,
        {"name": "苏俊宇·2026暑期数学计划", "target_type": "0", "target_id": su_id,
         "term_tag": "暑假", "year": 2026,
         "material_note": "学而思 36 周书 · 挑题制", "default_seg_template": G.DEFAULT_SEG_TEMPLATE, "status": "1"},
        G.LESSONS)
    plan_id = str(rp.get("plan_id"))
    lesson_ids = [str(x) for x in (rp.get("lesson_ids") or [])]
    step("③计划+13 课次（原样）", len(lesson_ids) == 13, f"plan={plan_id}")

    items = [{"date": d, "start": s, "end": e} for d, (s, e) in
             [(slot[0], (slot[1], slot[2])) for slot in G.SESSION_SLOTS]]
    rs = await S._schedule_sessions(client, "0", su_id, items, plan_id=plan_id, auto_bind=True, force=True)
    created = rs.get("created") or []
    step("③排 13 场（7月周二/周日+8月周日）", len(created) == 13, f"created={len(created)}")
    created.sort(key=lambda x: str(x.get("sessionDate") or x.get("date")))
    first_session = str(created[0].get("id") or created[0].get("sessionId"))

    # 段名/口诀 = 09a/09b/09c 原版逐字（打印件卷头与原版一致；note 空则卷头无副标题行）
    segs = [
        {"name": "第 1 次课 · ① 思维题", "style": "开场·换元/单位巧思", "question_ids": ids["mind"],
         "rules": "", "note": ""},
        {"name": "最值问题 · 专项练习", "style": "书挑题·★分层", "question_ids": ids["zx"],
         "rules": "第一层★7/第二层★★8/第三层★★★5选做",
         "note": "① 和一定：两数越接近，积越大；越悬殊，积越小。反过来——积一定，几个数越接近，和越小。"
                 "② 造大数：大数字往高位放；同时造两个数时，下一个大数字给当前较小的那个，让它们“又大又接近”。"},
        {"name": "课内过关 · 大数的认识", "style": "收尾过关·简单不费脑", "question_ids": ids["kn"],
         "rules": "", "note": ""},
    ]
    rb = await S._build_prep_pack(client, lesson_id=lesson_ids[0], segs=segs)
    pack_id = str(rb.get("pack_id"))
    rr = await S._render_prep_pack(client, pack_id, mark_ready=True)
    arts = rr.get("artifacts") or []
    # BUG-010 单文件拍板：1 个 artifact、pages>=3（三段各起新页）
    step("③第 1 次课备课包（真题 2+20+9）render 单文件",
         len(arts) == 1 and (arts[0].get("pages") or 0) >= 3,
         f"pack={pack_id} pages={[a.get('pages') for a in arts]}")

    item_results = [
        {"question_id": ids["mind"][0], "seg": "思维题", "seq": 1, "result": "对", "cause": ""},
        {"question_id": ids["mind"][1], "seg": "思维题", "seq": 2, "result": "对", "cause": ""},
        {"question_id": ids["zx"][2], "seg": "奥数专项·最值", "seq": 3, "result": "错", "cause": "计算"},
        {"question_id": ids["zx"][12], "seg": "奥数专项·最值", "seq": 13, "result": "卡", "cause": "策略"},
        {"question_id": ids["kn"][3], "seg": "课内过关", "seq": 4, "result": "错", "cause": "概念辨析"},
        {"question_id": ids["kn"][8], "seg": "课内过关", "seq": 9, "result": "对", "cause": ""},
    ]
    rv = await S._submit_review(client, first_session, item_results,
                                teacher_note="首课整体顺利：最值口诀吃住了；大数读写第4题读零规则再巩固")
    step("③第 1 场回收（家长消息+肖像delta）",
         bool(rv.get("ok") and str(rv.get("parent_msg", "")).startswith("家长您好")),
         (rv.get("parent_msg") or "")[:40].replace("\n", "/"))

    # ── ④ 5 名学生 + 周循环 4 周（R1a 新字段建档）──
    for name, label, gno, gyear, edition, subj, traits, desc, layer, weekly in STUDENTS:
        pr = {"traits": traits, "level": {"desc": desc, "target_layer": layer},
              "env": "", "history": [], "error_signals": []}
        rt = await S._create_teach_target(client, "student", name, grade_no=gno, grade_year=gyear,
                                          textbook_edition=edition, subject=subj,
                                          parent_phone="", profile=pr, color="")
        tid = str(rt.get("id"))
        its = []
        for w in range(WEEKS):
            for wd, (st, en) in weekly:
                d = WEEK_START + dt.timedelta(days=7 * w + wd)
                its.append({"date": d.isoformat(), "start": st, "end": en})
        rss = await S._schedule_sessions(client, "0", tid, its, auto_bind=False, force=True)
        step(f"④{name}（{label}）周循环 {len(weekly)} 节×{WEEKS} 周",
             len(rss.get("created") or []) == len(its), f"id={tid} created={len(rss.get('created') or [])}")

    # ── 验证：首周逐日节数 = 4/3/4/3/3/3/3（含苏俊宇周二/周日）──
    cal = await S._list_schedule(client, "2026-07-06", "2026-07-12")
    per = {}
    for s in cal.get("sessions", []):
        per[str(s.get("sessionDate"))] = per.get(str(s.get("sessionDate")), 0) + 1
    days = [(WEEK_START + dt.timedelta(days=i)).isoformat() for i in range(7)]
    counts = [per.get(d, 0) for d in days]
    step("④首周逐日节数(一~日)均在 3-4 节", all(3 <= c <= 4 for c in counts), f"{counts}")
    tg = await S._list_teach_targets(client)
    step("学生总数=6", len(tg.get("items") or []) == 6, f"n={len(tg.get('items') or [])}")

    await cluster.aclose()
    print("\n===== 演示数据重造 " + ("完成" if OK else "存在 FAIL") + " =====")
    print(f"苏俊宇 target={su_id} plan={plan_id} pack={pack_id} first_session={first_session}")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    asyncio.run(main())
