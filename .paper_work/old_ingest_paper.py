"""整卷批量录入：解析 omml_to_text 转出的卷子文本 → 经 MCP stdio client 真录入 biz_question + 组卷 biz_paper。

确定性拆题（题号、/【来源】/【答案】/【解析】/章节头）+ 逐题 login→[图自动传OSS]→format_question→ingest_question → create_paper。
🔴 图自动化：从原 docx 按 rId 抽图 → upload_image 传 OSS → 把 ossUrl 嵌进 stem 的 ![](url)（format 转图块）+ ingest_question.images（biz_question_image）。全代码完成，不手动传。

用法（cwd=teacher-mcp）:
  .venv\\Scripts\\python.exe tools\\ingest_paper.py --txt 卷.txt --docx 原卷.doc --subject-id 100 --batch ID --paper-name "卷名" --category-id 3001004004
"""
import argparse
import asyncio
import json
import os
import re
import sys
import zipfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.dicts import section_type_of  # noqa: E402  题型码单一事实源 = app/dicts.py（镜像 biz_question_type）
from tools.dbutil import errlog as _errlog  # noqa: E402  MCP 噪声日志引文件，控制台干净省 token
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
IMGDIR = ROOT / ".paper_imgs"

# 题号：N、 或 N． 或 N.（兼容两种命题排版）。(?![\d]) 排除小数(如 5.5G)、(?!\d*\s*分) 不误吃分值
QNUM = re.compile(r"^\s*(\d+)\s*[、．\.](?!\d)")
SECHDR = re.compile(r"^\s*[一二三四五六七八九十]+\s*[、．.]")
# 参考答案分隔：卷子后半段「参考答案与试题解析」用 【分析】/【解答】/【点评】 标记，与正文 【答案】/【解析】 不同口径
ANSWER_SECTION = re.compile(r"参考答案|试题解析|答案与解析")
OPT_LINE = re.compile(r"^\s*([A-DＡ-Ｄ])[\.．]")
FIG = re.compile(r"〖图(?::[^〗]*)?〗")          # 〖图:rId4,rId5〗 或 〖图〗
FIG_IDS = re.compile(r"〖图:([^〗]*)〗")          # 取 rId 列表
MD_IMG = re.compile(r"!\[\]\([^)]*\)")
# 来源行（题面开头残留的第二/多重来源）：含「第N题」标记(来源引用独有,真题干几乎不以此开头) 或 「学年…卷类型」。
# 不锚定行尾 → 抓「…A卷第10题4分」「…《有理数》第23题10分」这类多重来源第二行；真题干(如"2025年亚足联…")无「第N题」不误伤。
SRC_LINE = re.compile(r"第\s*\d+\s*题|学年.*(月考|期末|期中|模拟|联考|真题|单元测试|单元)")
SUBQ = re.compile(r"^\s*[（(]\s*\d+\s*[)）]")   # 小问标记 (1)(2)(3)，多小问题面分段用
BLANK = re.compile(r" {2,}|[ \t]{4,}")     # 填空空白(NBSP 连号 / ≥4 半角空格)→ 下划线，防渲染折叠丢失

# 🔴 题型码 = app/dicts.py 的 biz_question_type 镜像（全 8 类 1选择2判断3应用4填空5解答6作图7计算8证明），
#    用 section_type_of() 判型，不在此散落魔法值。

# 金标·地点：地名 → 国标行政区划 6 位码（市级；可 --region-code 兜底）
REGION_MAP = {
    "杭州": "330100", "宁波": "330200", "温州": "330300", "嘉兴": "330400", "湖州": "330500",
    "绍兴": "330600", "金华": "330700", "衢州": "330800", "舟山": "330900", "台州": "331000", "丽水": "331100",
    "广州": "440100", "深圳": "440300", "佛山": "440600", "顺德": "440600", "东莞": "441900",
    "南京": "320100", "建邺": "320100", "无锡": "320200", "徐州": "320300", "常州": "320400",
    "苏州": "320500", "南通": "320600", "扬州": "321000", "广陵": "321000", "镇江": "321100",
    "上海": "310100", "北京": "110100",
}


def derive_region(name):
    for kw, code in REGION_MAP.items():
        if kw in name:
            return code
    return ""


def derive_source_type(name):
    """1中考真题/2模拟/3期末/4月考/5单元/6自编/9其他。"""
    if "中考真题" in name:
        return 1
    if "模拟" in name or re.search(r"[一二三]模", name):
        return 2
    if "期末" in name:
        return 3
    if "月考" in name:
        return 4
    if "单元" in name or "章末" in name:
        return 5
    return 9


def derive_year(name):
    m = re.search(r"(20\d{2})\s*[~～\-]\s*(20\d{2})", name)
    if m:
        return m.group(2)  # 学年跨度 → 上学期期末在后一年（1月）
    m = re.search(r"(20\d{2})", name)
    return m.group(1) if m else ""


def dedup_key(pure_stem, batch, num):
    """去重键 = 题干纯字符（判断标准 = biz_question.stem_text）。
    够长的真题干 → 作 external_key 传底座，底座 md5 存 stem_hash：题干已在 biz_question 则命中、
    复用该题（updateById 不新建），create_paper 再把这道既有题加进本卷引用（biz_paper_question）。
    太短/占位（如纯图题『（见原卷）』）→ 回退 batch-题号 唯一键，避免不同图题被误并成一行。"""
    norm = re.sub(r"[\s，。、；：,.;:（）()【】\[\]？?！!]", "", pure_stem or "")
    return norm if len(norm) >= 10 else f"{batch}-{num}"


def split_inline_options(line):
    parts = re.split(r"(?=[A-DＡ-Ｄ][\.．])", line)
    out = []
    for p in parts:
        m = re.match(r"^[A-DＡ-Ｄ][\.．]\s*(.*)$", p.strip(), re.S)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def infer_type(stem, options, section_type_, block_joined):
    """无章节头时按题面兜底判型（题型码见 app/dicts.py·biz_question_type）。"""
    if options:
        return 1  # 选择
    if section_type_:
        return section_type_
    if re.search(r"求证|证明", block_joined):
        return 8  # 证明
    if re.search(r"[（(]\s*\d+\s*[)）]", stem) or re.search(r"解[：:]", block_joined):
        return 5  # 解答
    return 4  # 填空


def parse_block(num, block, section_type_):
    src = block[0]
    # 题号行内联题干兼容：N．（X分）题干…  → 剥题号 + 抽分值（（X分）），余下并入题面
    head = re.sub(r"^\s*\d+\s*[、．\.]\s*", "", src)
    score = 0.0
    hm = re.match(r"^[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[)）]\s*", head)
    if hm:
        score = float(hm.group(1))
        head = head[hm.end():]
    inline_body = [BLANK.sub("______", head)] if (head.strip() and "【来源】" not in src) else []
    body = inline_body + [BLANK.sub("______", l) for l in block[1:]]   # 填空空白 → 下划线（在 strip 前替，免被 strip 吃掉尾部空白）
    raw_src = src.split("【来源】", 1)[1].strip() if "【来源】" in src else ""
    if score == 0.0:
        sm = re.search(r"(\d+(?:\.\d+)?)\s*分\s*$", raw_src)
        score = float(sm.group(1)) if sm else 0.0
    source = re.sub(r"第\s*\d+\s*题\s*(?:\d+(?:\.\d+)?\s*分)?\s*$", "", raw_src).strip()

    # 🔴 状态机分段（修多小问截断）：题面结构 = [主干] (1)…【答案】…【解析】…(2)…【答案】…【解析】…
    #    遇【答案】/【解析】切到对应缓冲；遇新小问标记 (n) 切回题干 → 不再在首个【答案】处截断、(2)(3) 不丢。
    stem_buf, ans_buf, ana_buf = [], [], []
    mode = "stem"
    cur_sub = ""
    for l in body:
        if "【标注】" in l:
            break
        if "【答案】" in l:
            mode = "answer"
            seg = l.split("【答案】", 1)[1].strip()
            if seg:
                ans_buf.append((f"{cur_sub} " if cur_sub else "") + seg)
            continue
        if "【解析】" in l:
            mode = "analyze"
            seg = l.split("【解析】", 1)[1].strip()
            if seg:
                ana_buf.append((f"{cur_sub} " if cur_sub else "") + seg)
            continue
        m = SUBQ.match(l)
        if m:
            mode = "stem"                 # 新小问 → 回题干
            cur_sub = m.group(0).strip()
            stem_buf.append(l.strip())
            continue
        (stem_buf if mode == "stem" else ans_buf if mode == "answer" else ana_buf).append(l.strip())

    stem_lines, options = [], []
    for l in stem_buf:
        if OPT_LINE.match(l) or re.search(r"[A-DＡ-Ｄ][\.．].*[B-DＢ-Ｄ][\.．]", l):
            options.extend(split_inline_options(l))
        else:
            stem_lines.append(l)
    while stem_lines and SRC_LINE.search(stem_lines[0]):
        stem_lines.pop(0)
    stem = "\n".join(x for x in stem_lines if x).strip()
    answer = "\n".join(x for x in ans_buf if x).strip().rstrip(";；").strip()
    analyze = "\n".join(x for x in ana_buf if x).strip()   # 保留 〖图〗 标记，run() 再传图
    block_joined = "\n".join(block)
    has_fig = bool(FIG.search(block_joined))
    qtype = infer_type(stem, options, section_type_, block_joined)
    return {"num": num, "type": qtype, "stem": stem, "options": options,
            "answer": answer, "analyze": analyze, "has_fig": has_fig,
            "source": source, "score": score}


def parse_answer_section(lines):
    """解析卷尾「参考答案与试题解析」段（每题 N．【分析】…【解答】…【点评】…）。
    返回 {num: {"answer": 末答(故选/故答案), "analyze": 分析+解答全文}}，回填到正文题。"""
    ans = {}
    i = 0
    while i < len(lines):
        m = QNUM.match(lines[i])
        if not m:
            i += 1
            continue
        num = int(m.group(1))
        block = [lines[i]]
        i += 1
        while i < len(lines):
            if QNUM.match(lines[i]) or (SECHDR.match(lines[i]) and not QNUM.match(lines[i])):
                break
            block.append(lines[i])
            i += 1
        joined = "\n".join(block)
        # 末答 = 故选：/故答案为：行（选择/填空），否则取【解答】整段当 answer
        body = re.sub(r"^\s*\d+\s*[、．\.]\s*", "", joined)  # 去题号前缀
        # 拆 【分析】/【解答】/【点评】
        seg = re.split(r"(【分析】|【解答】|【点评】)", body)
        buf = {"【分析】": "", "【解答】": "", "【点评】": ""}
        cur = None
        for piece in seg:
            if piece in buf:
                cur = piece
            elif cur:
                buf[cur] += piece
        solve = buf["【解答】"].strip()
        fenxi = buf["【分析】"].strip()
        final = ""
        fm = re.findall(r"(故选[：:]\s*[A-DＡ-Ｄ]．?|故答案为[：:][^\n]*)", solve)
        if fm:
            final = fm[-1].strip().rstrip("．.")
        # answer 优先用末答；analyze = 分析 + 解答（含点评略去，保留过程）
        ana_parts = []
        if fenxi:
            ana_parts.append("【分析】" + fenxi)
        if solve:
            ana_parts.append("【解答】" + solve)
        ans[num] = {"answer": final or solve, "analyze": "\n".join(ana_parts)}
    return ans


def parse_paper(text):
    lines = text.split("\n")
    # 切分正文 / 参考答案段：第一处「参考答案与试题解析」之后归答案段
    cut = next((k for k, l in enumerate(lines) if ANSWER_SECTION.search(l)), None)
    ans_map = {}
    if cut is not None:
        ans_map = parse_answer_section(lines[cut:])
        lines = lines[:cut]
    out = []
    cur_type = None
    i = 1 if lines and not QNUM.match(lines[0]) else 0
    while i < len(lines):
        line = lines[i]
        if SECHDR.match(line) and not QNUM.match(line):
            t = section_type_of(line)
            if t:
                cur_type = t
            i += 1
            continue
        m = QNUM.match(line)
        if m:
            num = int(m.group(1))
            block = [line]
            i += 1
            while i < len(lines):
                if QNUM.match(lines[i]) or (SECHDR.match(lines[i]) and not QNUM.match(lines[i])):
                    break
                block.append(lines[i])
                i += 1
            q = parse_block(num, block, cur_type)
            # 正文无答案/解析时，回填卷尾参考答案段
            if num in ans_map:
                if not q["answer"]:
                    q["answer"] = ans_map[num]["answer"]
                if not q["analyze"]:
                    q["analyze"] = ans_map[num]["analyze"]
            if q["stem"] or q["options"]:
                out.append(q)
            continue
        i += 1
    return out


# ───────────────────────── 图片：docx 抽图 + 上传缓存 ─────────────────────────

def load_docx_media(docx_path):
    """打开 docx，返回 (zipfile, {rId: 内部media路径})。🔴 Id/Target 顺序不固定，逐 tag 解析。"""
    z = zipfile.ZipFile(docx_path)
    rels = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
    rid_map = {}
    for tag in re.findall(r"<Relationship\b[^>]*/>", rels):
        idm = re.search(r'Id="(rId\d+)"', tag)
        tm = re.search(r'Target="([^"]+)"', tag)
        if idm and tm:
            rid_map[idm.group(1)] = tm.group(1)
    return z, rid_map


def _unwrap(result):
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    for c in getattr(result, "content", []) or []:
        if getattr(c, "text", None):
            try:
                return json.loads(c.text)
            except Exception:
                return {"_text": c.text}
    return {}


def plain_text(s):
    """题干纯文本（去 markdown 图 + 残留 〖图〗 标记）。"""
    s = MD_IMG.sub("", s)
    s = FIG.sub("", s)
    return re.sub(r"\n{2,}", "\n", s).strip()


async def run(args):
    text = Path(args.txt).read_text(encoding="utf-8")
    questions = parse_paper(text)
    if args.limit:
        questions = questions[: args.limit]
    figs = sum(1 for q in questions if q["has_fig"])
    print(f"[parse] {len(questions)} 题（含图 {figs} 题）")
    if args.dry:
        for q in questions[:5]:
            print(json.dumps(q, ensure_ascii=False)[:300])
        return

    # docx 抽图准备
    z = rid_map = None
    if args.docx and os.path.exists(args.docx):
        IMGDIR.mkdir(exist_ok=True)
        z, rid_map = load_docx_media(args.docx)
        print(f"[img] docx 媒体就绪，rId 映射 {len(rid_map)} 个")
    else:
        print("[img] 未给 --docx 或文件不存在 → 跳过传图（图占位）")

    paper_name = args.paper_name or (text.split("\n", 1)[0].strip() if text else args.batch)
    # 金标：年份 + 地点 + 类型（卷名派生，可 --exam-year/--region-code/--source-type 兜底），每题继承
    gb_year = args.exam_year or derive_year(paper_name)
    gb_region = args.region_code or derive_region(paper_name)
    gb_type = args.source_type or derive_source_type(paper_name)
    print(f"[金标] 年份={gb_year or '?'} 地点={gb_region or '?'} 类型={gb_type}（1中考/2模拟/3期末/4月考/5单元）")
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    ok, fail, reused, ids = 0, 0, 0, []
    img_cache = {}     # rId -> ossUrl
    uploaded = 0

    async with stdio_client(server, errlog=_errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok"), f"login 失败 {login}"
            print(f"[login] teacher_id={login.get('teacher_id')}")

            async def resolve_rid(rid):
                """rId → (ossUrl, assetId)（抽 media 写本地 → upload_image 传 OSS，缓存）。"""
                nonlocal uploaded
                if rid in img_cache:
                    return img_cache[rid]
                url = aid = None
                tgt = (rid_map or {}).get(rid) if rid_map else None
                if tgt and z is not None:
                    try:
                        data = z.read("word/" + tgt.replace("\\", "/"))
                        ext = os.path.splitext(tgt)[1] or ".png"
                        fp = IMGDIR / f"{args.batch}_{rid}{ext}"
                        fp.write_bytes(data)
                        up = _unwrap(await session.call_tool(
                            "upload_image", {"local_path": str(fp), "asset_kind": "figure"}))
                        if up.get("ok"):
                            url = up.get("oss_url")
                            aid = up.get("asset_id")
                            uploaded += 1
                    except Exception as e:
                        print(f"  ⚠ 图 {rid} 传失败 {type(e).__name__}")
                img_cache[rid] = (url, aid)
                return url, aid

            async def imagify(s, role, meta):
                """把 s 里的 〖图:rId..〗 替换成 ![](ossUrl)，并收集 images meta（含 assetId，否则底座跳过 biz_question_image）。"""
                for m in list(FIG_IDS.finditer(s)):
                    rids = [x.strip() for x in m.group(1).split(",") if x.strip().startswith("rId")]
                    repl = ""
                    for rid in rids:
                        u, aid = await resolve_rid(rid)
                        if u:
                            repl += f"![]({u})"
                            meta.append({"ossUrl": u, "assetId": aid, "role": role})
                    s = s.replace(m.group(0), repl, 1)
                s = FIG.sub("", s)  # 清掉无 rId 的 〖图〗
                return s

            for q in questions:
                images_meta = []
                stem_md = await imagify(q["stem"], "stem", images_meta)
                opts_md = [await imagify(o, "figure", images_meta) for o in q["options"]]
                analyze_md = await imagify(q["analyze"], "analysis", images_meta)

                block_json = ""
                fmt = _unwrap(await session.call_tool("format_question", {
                    "question_type": q["type"], "stem": stem_md, "options": opts_md}))
                if fmt.get("ok"):
                    block_json = fmt.get("block_json") or ""

                ing = _unwrap(await session.call_tool("ingest_question", {
                    "subject_id": args.subject_id,
                    "question_type": q["type"],
                    "difficult": 2,
                    "stem_text": plain_text(stem_md) or "（见原卷）",
                    "block_json": block_json,
                    "answer_text": q["answer"],
                    "analyze_text": analyze_md,
                    "images": images_meta,
                    # 🔴 去重键=题干纯字符：同题干→复用既有题、仅加本卷引用，不新建重复行
                    "external_key": dedup_key(plain_text(stem_md), args.batch, q["num"]),
                    "exam_year": gb_year,
                    "region_code": gb_region,
                    "source_type": gb_type,
                    "source_raw": q["source"] or paper_name,
                    "status": "1",
                }))
                if ing.get("ok"):
                    ok += 1
                    ids.append(ing.get("question_id"))
                    if ing.get("created") is False:
                        reused += 1
                        print(f"  ♻ 第{q['num']}题 题干已存在 → 复用 qid={ing.get('question_id')}，仅加本卷引用")
                else:
                    fail += 1
                    print(f"  ❌ 第{q['num']}题 {ing.get('reason')}")
    print(f"[done] batch={args.batch} ok={ok}(去重复用{reused}) fail={fail} total={len(questions)} 传图={uploaded}")

    if ids and not args.no_paper:
        async with stdio_client(server, errlog=_errlog()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("login", {})
                cp = _unwrap(await session.call_tool("create_paper", {
                    "name": paper_name, "question_ids": [int(x) for x in ids],
                    "paper_category_id": args.category_id or ""}))
                if cp.get("ok"):
                    pid = cp.get("paper_id")
                    # 🔴 page() 按 p.subject_id likeRight 筛目录，/create 不设 subject_id → 脚本补设=目录节点 id，卷才在目录下可见
                    if args.category_id and pid:
                        _set_paper_subject(pid, args.category_id)
                    # 分值(通值)+ 建议时长：走 update_paper 按年级标准分算
                    up = _unwrap(await session.call_tool("update_paper", {
                        "paper_id": int(pid), "total_score": args.total_score, "suggest_time": args.suggest_time}))
                    sc = f"总分{up.get('total')}" if up.get("ok") else f"分值失败:{up.get('reason')}"
                    print(f"[paper] ✅ 建卷 paper_id={pid} 「{paper_name}」 {cp.get('question_count')}题 cat={args.category_id} {sc} 时长{args.suggest_time}min")
                else:
                    print(f"[paper] ❌ {cp.get('reason')}")


def _set_paper_subject(paper_id, category_id):
    """建卷后补设 biz_paper.subject_id=目录节点 id（page 按它筛目录）。dev 库本地维护。"""
    try:
        import pymysql
        c = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="123456",
                            database="ai_lesson_prep", charset="utf8mb4")
        with c.cursor() as cur:
            cur.execute("UPDATE biz_paper SET subject_id=%s WHERE id=%s", (str(category_id), int(paper_id)))
            c.commit()
        c.close()
    except Exception as e:
        print(f"  ⚠ subject_id 补设失败 {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", required=True)
    ap.add_argument("--docx", default="")
    ap.add_argument("--subject-id", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--paper-name", default="")
    ap.add_argument("--category-id", default="")
    ap.add_argument("--exam-year", default="")      # 金标·年份兜底
    ap.add_argument("--region-code", default="")    # 金标·地点兜底（国标行政区划）
    ap.add_argument("--source-type", type=int, default=0)  # 类型兜底
    ap.add_argument("--total-score", type=int, default=120)   # 试卷总分(通值，初中常规120)
    ap.add_argument("--suggest-time", type=int, default=120)  # 建议时长(分钟)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-paper", action="store_true")
    ap.add_argument("--dry", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
