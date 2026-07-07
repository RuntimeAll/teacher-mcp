"""确定性拆题引擎（单一事实源）—— 从 tools/ingest_paper.py 上提（PRD-C-208 ⑤ 管线迁移）。

把 omml_to_text 转出的卷子文本确定性拆成题列表：题号/章节头/【来源】/【答案】/【解析】/
多小问状态机/选项/分值/金标派生。tools/ingest_paper.py 与 MCP 工具 parse_paper_text 共用本模块，
**逻辑逐行等价迁移（G9 等价回归的前提），改拆题规则只改这里**。
"""
import re
import zipfile

from teacher_mcp.domains.dicts import section_type_of

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
BLANK = re.compile(" {2,}|[ 	]{4,}")     # 填空空白(NBSP 连号   / ≥4 半角空格)→ 下划线（显式   转义，勿改回字面空格）

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


# 来源前缀（题干开头的「（真题·杭州滨江）」「（2025 浙江期末）」「【2024 中考】」类残留）——
# 🔴 关键词门控保守剥离：括号段必须含来源词才剥，纯数学括号（如「(0°<α<180°)」不在开头也不含词）绝不误伤。
_SRC_WORDS = r"(?:真题|中考|高考|会考|竞赛|模拟|期末|期中|月考|学年|单元测试|质检|调研|联考|检测|专题练习|假期作业|20\d{2})"
SOURCE_PREFIX = re.compile(
    r"^\s*(?:[（(【\[][^）)】\]]*" + _SRC_WORDS + r"[^）)】\]]*[）)】\]][·．.、\s]*)+")


def strip_source_prefix(stem):
    """剥题干开头的来源前缀。返回 (干净题干, 被剥内容或 '')。灌库前缀清洗铁律的预防端（防残留 REGEXP>0）。"""
    m = SOURCE_PREFIX.match(stem or "")
    if not m:
        return stem, ""
    return stem[m.end():].lstrip(), m.group(0).strip()


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
    analyze = "\n".join(x for x in ana_buf if x).strip()   # 保留 〖图〗 标记，调用侧再传图
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


def plain_text(s):
    """题干纯文本（去 markdown 图 + 残留 〖图〗 标记）。"""
    s = MD_IMG.sub("", s)
    s = FIG.sub("", s)
    return re.sub(r"\n{2,}", "\n", s).strip()


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


def make_digest(qs, stem_width=0):
    """题面摘要（每题 1 行）：#位置序号 t题型 [🖼] 分值 | 题干 [选项] =答:…。
    🔴 用位置序号 i(1..N)=DB sort（入库按 parse 顺序编号），不用 q["num"]（原卷题号遇杂散「N.」会错位）。
    stem_width>0 时题干截断（sync 轻管线 90 字口径），=0 不截（run_paper 全文口径）。"""
    out = []
    for i, q in enumerate(qs, 1):
        opt = "  [" + " | ".join(q["options"]) + "]" if q["options"] else ""
        stem = plain_text(q["stem"]).replace("\n", " ")
        if stem_width:
            stem = stem[:stem_width]
            opt = opt[:60]
        fig = "🖼" if q["has_fig"] else "  "
        ans = q["answer"][:40] if stem_width else q["answer"][:50]
        out.append(f'#{i:>2} t{q["type"]}{fig}{int(q["score"])}分 | {stem}{opt}  =答:{ans}')
    return "\n".join(out)
