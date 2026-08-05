# -*- coding: utf-8 -*-
"""PRD-016 教务速办机器人 · 执行层（prod BE 契约 + 反馈文案生成 + 预览/执行）。

分层：intents.py 只翻译，本模块只执行系统契约，jiaowu_bot.py 只管飞书收发。
🔴 核心理念：LLM 永远不直接碰数据——claude CLI 的产出只允许进反馈单五列字段。

已实证环境事实（勿改）：
- BE = http://www.jpjia.cn/api（101 → jpjia.cn **只通 http**）
- 双制式 envelope：/auth/** = {code:200,msg,data}；/teacher/** = {code:1,message,response}
- 鉴权双头：Authorization: Bearer <tok> + clientid: <RUOYI_CLIENT_ID>
- botLogin：POST /auth/botLogin {openid,clientId} + header X-Bot-Secret
- 101 shell 带 mihomo 代理变量 → 所有外发一律 ProxyHandler({}) 直连
"""

import io
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

RUOYI = os.environ.get("RUOYI_BASE_URL_HTTP", "http://www.jpjia.cn/api")
H5_BASE = os.environ.get("H5_BASE", "http://jpjia.cn/#/m")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLI_TIMEOUT = int(os.environ.get("CLI_TIMEOUT", "300"))
ROSTER_TTL = 120          # 花名册缓存秒数
TOKEN_TTL = 25 * 60       # token 自保守过期，到点重签

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SUBJECT_CODE = {"数学": "1", "科学": "2", "语文": "3", "英语": "4"}


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_env(path):
    d = {}
    try:
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


class BeError(Exception):
    pass


# ───────────────────────────── BE 客户端 ─────────────────────────────


class Be(object):
    """prod BE 教务域客户端。botLogin 免密签发 + 双头鉴权 + 双制式 envelope 解包。"""

    def __init__(self, bot_openid, bot_secret, client_id=None):
        self.bot_openid = bot_openid
        self.bot_secret = bot_secret
        self.client_id = client_id
        self._tok = None
        self._tok_ts = 0
        self._roster = None
        self._roster_ts = 0

    # ---- 底层 ----

    def _raw(self, path, body=None, headers=None, method=None, timeout=60):
        url = RUOYI + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise BeError("HTTP %s %s %s" % (e.code, path, txt))
        except Exception as e:
            raise BeError("%s %s" % (path, repr(e)[:200]))

    def token(self, force=False):
        if self._tok and not force and (time.time() - self._tok_ts) < TOKEN_TTL:
            return self._tok
        body = {"openid": self.bot_openid}
        if self.client_id:
            body["clientId"] = self.client_id
        r = self._raw("/auth/botLogin", body, {"X-Bot-Secret": self.bot_secret})
        if r.get("code") != 200 or not (r.get("data") or {}).get("access_token"):
            raise BeError("botLogin 失败：%s" % str(r)[:200])
        self._tok = r["data"]["access_token"]
        self._tok_ts = time.time()
        log("botLogin ok")
        return self._tok

    def _auth_headers(self):
        h = {"Authorization": "Bearer " + self.token()}
        if self.client_id:
            h["clientid"] = self.client_id
        return h

    def call(self, path, body=None, method=None, retry=True):
        """/teacher/** 调用 → 解 envelope 返 response；失败抛 BeError。401 自动重签一次。"""
        r = self._raw(path, body, self._auth_headers(), method)
        code = r.get("code")
        if code in (401, 403) and retry:
            self.token(force=True)
            return self.call(path, body, method, retry=False)
        if code != 1:
            raise BeError(r.get("message") or r.get("msg") or ("code=%s" % code))
        return r.get("response")

    def download(self, url_path, timeout=120):
        """产物下载（🔴 artifact 端点要 token，裸 GET 会 401）→ bytes。"""
        req = urllib.request.Request(RUOYI + url_path)
        for k, v in self._auth_headers().items():
            req.add_header(k, v)
        with OPENER.open(req, timeout=timeout) as r:
            return r.read()

    # ---- 花名册 ----

    def roster(self, force=False):
        if self._roster is not None and not force and (time.time() - self._roster_ts) < ROSTER_TTL:
            return self._roster
        resp = self.call("/teacher/schedule/target/page") or {}
        self._roster = resp.get("rows") or []
        self._roster_ts = time.time()
        return self._roster

    def account_of(self, student, subject_label):
        """从花名册行里挑该学科账户；subject_label 为空且只有一个账户则取它。"""
        accs = student.get("accounts") or []
        if subject_label:
            for a in accs:
                if a.get("subjectLabel") == subject_label:
                    return a
        return accs[0] if len(accs) == 1 else None

    def plan_of(self, student, subject_label=None):
        for b in (student.get("planBindings") or []):
            if not subject_label or b.get("subjectLabel") == subject_label:
                return b
        return None

    # ---- 只读查询 ----

    def pending(self):
        return self.call("/teacher/schedule/settle/pending") or []

    def ledger(self, account_id, page_size=8):
        return self.call("/teacher/schedule/account/%s/ledger?pageSize=%d"
                         % (account_id, page_size)) or {}

    def sessions(self, target_id):
        return (self.call("/teacher/schedule/session/page?targetId=%s" % target_id) or {}).get("rows") or []

    def sheets(self, target_id):
        return (self.call("/teacher/feedback/sheet/page?targetId=%s" % target_id) or {}).get("rows") or []

    # ---- 写（只由确认闸放行后调用） ----

    def upsert_account(self, student_id, subject_label, price_per_hour, hours_per_lesson=1.0, note=None):
        """PRD-018 v3：开户=建账本+绑定；price_per_hour=时薪(元/小时)，hours_per_lesson=每节时长(小时)。"""
        body = {"studentId": str(student_id), "subject": SUBJECT_CODE.get(subject_label, subject_label),
                "pricePerHour": price_per_hour, "hoursPerLesson": hours_per_lesson}
        if note:
            body["note"] = note
        return self.call("/teacher/schedule/account", body)

    def add_flow(self, account_id, flow_type, hours=0, amount=0, lessons=0, occur_date=None, note=None):
        """PRD-018 三档任一（hours 小时 / lessons 节 / amount 元），BE 按账本时薪与每节时长互换算。
        occur_date=业务日期（补录历史充值回填真实日期，缺省=今天）。"""
        body = {"flowType": flow_type}
        if hours:
            body["hours"] = hours
        if lessons:
            body["lessons"] = lessons
        if amount:
            body["amount"] = amount
        if occur_date:
            body["occurDate"] = str(occur_date)
        if note:
            body["note"] = note
        return self.call("/teacher/schedule/account/%s/flow" % account_id, body)

    def settle(self, items, gen_feedback=False):
        return self.call("/teacher/schedule/settle",
                         {"items": items, "genFeedback": bool(gen_feedback)})

    def session_batch(self, target_id, items, plan_id=None, force=True):
        """批量建场次（⑧ 补录用）→ {'created':[sessionVo], 'conflicts':[...]}。

        🔴 targetType 必须 '0'（学生）——实证过 BE：'1' 是**班级**，
        班课既进不了 /settle/pending，settleOne 也直接抛「班课暂不支持结算」，
        补录出来的场次会变成永远结不掉的孤儿。
        🔴 planId=None 时服务端 autoBind 自动失效（batch() 里 `autoBind && planId != null`），
        不会去抢占课程计划的未排课次——补录只补「上过的课」，不动备课线。
        🔴 force=True：重复防线由调用方**建之前查同日同时段**负责（见 _op_ingest_lesson_log），
        不靠服务端冲突检测——它 force=False 时是「有一条冲突就整批不建」，粒度太粗。
        """
        body = {"targetType": "0", "targetId": str(target_id), "planId": plan_id,
                "autoBind": False, "force": bool(force), "items": items}
        return self.call("/teacher/schedule/session/batch", body) or {}

    def export_ledger_png(self, account_id):
        """课时流水单 PNG → {'file','url'}。

        🔴 真实 path = /export-ledger-png（不是 /ledger-png，已回 TuitionAccountController 核实）。
        """
        return self.call("/teacher/schedule/account/%s/export-ledger-png" % account_id, {})

    def leave(self, session_id):
        return self.call("/teacher/schedule/session/%s/leave" % session_id, {})

    def upsert_sheet(self, bo, sheet_id=None):
        if sheet_id:
            self.call("/teacher/feedback/sheet/%s" % sheet_id, bo, method="PUT")
            return {"id": str(sheet_id)}
        return self.call("/teacher/feedback/sheet", bo)

    def export_sheet_png(self, sheet_id):
        return self.call("/teacher/feedback/sheet/%s/export-png" % sheet_id, {})

    def export_plan_png(self, plan_id, mode="single"):
        return self.call("/teacher/feedback/export-plan-png",
                         {"planId": str(plan_id), "mode": mode})


# ───────────────────────── 反馈文案生成（walled LLM） ─────────────────────────

_WALL_PROMPT = """你是「课后反馈单」结构化生成器。你的唯一输出 = 一个 JSON 对象，除 JSON 外不要任何文字。

输出格式（严格）：
{"rows":[{"seq":1,"module":"...","content":"...","mastery":"...","weakness":"..."}]}

字段口径：
- seq     序号，从 1 递增
- module  所属模块，2-6 字（如 计算 / 思维 / 课内同步 / 口算 / 应用题）
- content 学习内容，一句家长能看懂的话
- mastery 只能是「熟练」「基本掌握」「待巩固」三者之一
- weakness 不足点；确实没有就写 —

🔴 铁律：
1. 只输出 JSON，不要解释、不要 markdown 代码块、不要前后缀。
2. 家长可见：绝不出现内部词——层 / ★ / 基础过关 / 强化 / 拓展 / 素材 / 挑题 / 薄弱。
3. 只写材料里真实出现的内容，缺信息就少写一行，绝不编造。
4. 行数 3~8 行。

🔴 工具与产出的关系（务必看清，别搞反）：
- 「只输出 JSON」约束的是你的**最终回答**，**不**约束中间的工具调用。
- 任务里若给了图片路径，你**必须**先用 Read 工具把每一张逐张读完再动笔——
  这一步是允许的、必需的；跳过它直接交空 rows 是错误行为。
- 读完之后，最终回答只留那一个 JSON 对象。"""

_MASTERY_OK = ("熟练", "基本掌握", "待巩固")
_BANNED = ("层", "★", "基础过关", "强化", "拓展", "素材", "挑题", "薄弱")


def _extract_json(text):
    """从 CLI 输出里抠出第一个 {...} JSON 对象（容忍代码块/寒暄）。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else None
    if not cand:
        i = text.find("{")
        j = text.rfind("}")
        cand = text[i:j + 1] if i >= 0 and j > i else None
    if not cand:
        return None
    try:
        return json.loads(cand)
    except Exception:
        return None


def _sanitize(rows):
    """五列净化：字段收口 + 掌握情况归一 + 内部词剥除（家长可见铁律）。"""
    out = []
    for i, r in enumerate(rows or [], 1):
        if not isinstance(r, dict):
            continue
        mastery = str(r.get("mastery") or "").strip()
        if mastery not in _MASTERY_OK:
            mastery = "基本掌握"
        row = {
            "seq": i,
            "module": str(r.get("module") or "课堂").strip()[:12],
            "content": str(r.get("content") or "").strip()[:200],
            "mastery": mastery,
            "weakness": (str(r.get("weakness") or "").strip() or "—")[:120],
        }
        for k in ("module", "content", "weakness"):
            for w in _BANNED:
                row[k] = row[k].replace(w, "")
            row[k] = row[k].strip() or ("—" if k == "weakness" else "课堂")
        if row["content"]:
            out.append(row)
        if len(out) >= 8:
            break
    return out


def _run_claude(prompt, image_paths=None, timeout=None, system=None):
    """headless claude CLI（订阅，不引入新 LLM key）。失败/超时返 ''。

    🔴 walled：不挂任何 MCP，只放 Read（读本地图片）；产出只准进结构化字段，
    绝不直接当动作执行——写库永远要过 run_op + 确认闸。

    system = 本次的 wall（默认 = 反馈五列的 _WALL_PROMPT）。
    brain.py 的路由 pass / 读图 pass 各带自己的 wall 走这里，共用同一条 CLI 通路。
    """
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--allowedTools", "Read", "--append-system-prompt", system or _WALL_PROMPT]
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    if model:
        cmd += ["--model", model]
    try:
        # 🔴 必须显式 encoding="utf-8"：text=True 走的是**系统 locale**，
        #    非 UTF-8 环境（Windows GBK，或 systemd 没带 LANG 的裸环境）读中文输出会
        #    UnicodeDecodeError 崩在 subprocess 的读线程里 → 这里拿到空串 → 静默降级，
        #    看起来就像"模型没跑通"。errors="replace" 保证再脏的字节也不炸。
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout or CLI_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        log("claude CLI 超时 %ss" % (timeout or CLI_TIMEOUT))
        return ""
    except Exception as e:
        log("claude CLI 起不来：%r" % e)
        return ""
    if p.returncode != 0:
        log("claude CLI rc=%s err=%s" % (p.returncode, (p.stderr or "")[:200]))
    try:
        ev = json.loads(p.stdout or "{}")
        # 可观测：turns=1 且带图 = 模型没读图直接交卷（踩过一次，日志里要能一眼看出）
        log("claude 完成 model=%s turns=%s dur=%ss cost=$%s"
            % (",".join(sorted((ev.get("modelUsage") or {}).keys())) or "?",
               ev.get("num_turns"), round((ev.get("duration_ms") or 0) / 1000.0, 1),
               ev.get("total_cost_usd")))
        return str(ev.get("result") or "")
    except Exception:
        return p.stdout or ""


# 🔴 对外别名：brain.py（理解层）复用同一条 CLI 通路与同一个 JSON 抠取器，
#    避免两处各写一份、行为漂移。下划线版保留给本模块内部老调用点。
run_claude = _run_claude
extract_json = _extract_json


def _fallback_rows(brief):
    """CLI 失败/超时兜底：不阻断老师，按模板出一行，标注需手改。"""
    txt = re.sub(r"\s+", " ", (brief or "").strip())[:180] or "本节课堂内容"
    parts = [x for x in re.split(r"[；;。\n]+", txt) if x.strip()][:5]
    rows = [{"seq": i, "module": "课堂", "content": p.strip(),
             "mastery": "基本掌握", "weakness": "—"} for i, p in enumerate(parts, 1)]
    return rows or [{"seq": 1, "module": "课堂", "content": txt,
                     "mastery": "基本掌握", "weakness": "—"}]


def gen_feedback_rows(brief, student_name, lesson_date, image_paths=None):
    """→ (rows, source)；source ∈ {'llm','fallback'}。"""
    imgs = image_paths or []
    if imgs:
        listing = "\n".join(imgs)
        prompt = ("[任务] 老师发来 %d 张学生作业/板书照片，本地路径如下。"
                  "🔴 必须用 Read 工具逐张读完全部 %d 张，一张都不能跳过；"
                  "一张图里可能上下排布多个表格，每个都要看完。\n%s\n\n"
                  "[学生] %s\n[上课日期] %s\n[老师补充] %s\n\n"
                  "据以上材料输出课后反馈单五列 JSON。"
                  % (len(imgs), len(imgs), listing, student_name, lesson_date, brief or "（无）"))
        timeout = CLI_TIMEOUT
    else:
        prompt = ("[学生] %s\n[上课日期] %s\n[老师口述的本节要点]\n%s\n\n"
                  "据以上要点输出课后反馈单五列 JSON。"
                  % (student_name, lesson_date, brief or "（无）"))
        timeout = min(CLI_TIMEOUT, 180)
    raw = _run_claude(prompt, imgs, timeout)
    obj = _extract_json(raw)
    rows = _sanitize((obj or {}).get("rows") if isinstance(obj, dict) else obj)
    if rows:
        return rows, "llm"
    log("反馈文案 LLM 未产出可用 JSON，降级模板（raw=%s）" % (raw or "")[:160])
    return _sanitize(_fallback_rows(brief)), "fallback"


# ───────────────────────────── 预览与执行 ─────────────────────────────
#
# 每个写操作 = 一个 op：build_* 出「预览文本 + args」，run_* 在确认后真执行。
# 🔴 run_* 只允许被 jiaowu_bot 的确认闸调用。


def money(v):
    if v is None:
        return "—"
    f = float(v)
    return ("%.2f" % f).rstrip("0").rstrip(".")


def h5(page):
    return "%s/%s" % (H5_BASE, page)


def rows_text(rows):
    return "\n".join("  %s. [%s] %s ｜ %s ｜ 不足：%s"
                     % (r["seq"], r["module"], r["content"], r["mastery"], r["weakness"])
                     for r in rows)


def run_op(be, op, args):
    """确认后真执行 → {'text':..., 'image_path':(BE 产物路径)|None}。"""
    fn = _OPS.get(op)
    if not fn:
        raise BeError("未知操作 %s" % op)
    return fn(be, args)


# ---- ① 开户 / 改时薪（可带顺手充值；PRD-018 v3=建账本+绑定） ----

def is_shared(acc):
    """这本账是不是**共享账本**（绑定学生>1，如俊羽+好好并本）。

    🔴 字段名以 BE 实测为准（TuitionAccountService.accountVo/accountBookVo）：
       学生视角 VO 给 `shared`(bool)，账本视角 VO 给 `bindingCount`。
       曾经写的 `bindCount` 是**不存在的键**，共享判定会永远为假 → 共享本被折成节。
    """
    if not acc:
        return False
    if acc.get("shared") is not None:
        return bool(acc.get("shared"))
    return int(acc.get("bindingCount") or acc.get("bindCount") or 1) > 1


def bal_text(acc):
    """余额正文（双单位，PRD-018 D1 v2.1）：小时为底 + 折节；共享本只报小时。

    折节优先用 BE 直接给的 `lessonsRemain`（同一处口径），拿不到才本地按每节时长折。
    共享账本每人每节时长不同、基准不唯一 → 只报小时（D4 规则②）。
    """
    if not acc:
        return "—"
    h = acc.get("hoursRemain")
    if not is_shared(acc):
        lessons = acc.get("lessonsRemain")
        if lessons is None and acc.get("hoursPerLesson"):
            try:
                lessons = float(h) / float(acc["hoursPerLesson"])
            except Exception:
                lessons = None
        if lessons is not None:
            return "%s 小时（≈%s 节）" % (money(h), money(lessons))
    return "%s 小时" % money(h)


def _bal_line(acc):
    return ("📊 当前余额：" + bal_text(acc)) if acc else ""


def _op_account_open(be, a):
    r = be.upsert_account(a["studentId"], a["subject"], a["price"],
                          a.get("hoursPerLesson") or 1.0, a.get("note"))
    acc_id = (r or {}).get("id")
    hpl_v = a.get("hoursPerLesson") or 1.0
    lines = ["✅ %s · %s 账本已就绪（每节 %s 小时 · %s 元/节）"
             % (a["studentName"], a["subject"], money(hpl_v), money(round(float(a["price"]) * float(hpl_v), 2)))]
    if a.get("hours") or a.get("amount") or a.get("lessons"):
        be.add_flow(acc_id, "1", hours=a.get("hours") or 0, amount=a.get("amount") or 0,
                    lessons=a.get("lessons") or 0, occur_date=a.get("occurDate"),
                    note=a.get("note") or "机器人充值")
        lines.append("✅ 已充值（%s）" % _flow_brief(a))
    be.roster(force=True)
    for st in be.roster():
        if str(st.get("id")) == str(a["studentId"]):
            acc = be.account_of(st, a["subject"])
            if acc:
                lines.append(_bal_line(acc))
    lines.append("👉 %s" % h5("account"))
    return {"text": "\n".join(lines), "image_path": None}


def _flow_brief(a):
    """充值/调整三档任一的回执片段：说老师给的那一档，别替 BE 算换算。

    🔴 档位优先级与 BE 的 resolveHours 一致：hours → lessons → amount。
    调整场景是负数，所以符号要跟着值走（不能无脑 "+"）。
    """
    for key, unit in (("hours", "小时"), ("lessons", "节"), ("amount", "元")):
        v = a.get(key)
        if v:
            return "%s%s %s" % ("+" if float(v) >= 0 else "", money(v), unit)
    return "+0 元"


# 🔴 对外别名：jiaowu_bot 的预览段与本模块的回执段共用同一套档位话术，
#    两处各写一份必漂移（预览说「+20 节」、回执说「+30 小时」这种）。
flow_brief = _flow_brief


# ---- ① 充值 / 调整 ----

def _op_account_flow(be, a):
    be.add_flow(a["accountId"], a["flowType"], hours=a.get("hours") or 0,
                amount=a.get("amount") or 0, lessons=a.get("lessons") or 0,
                occur_date=a.get("occurDate"),
                note=a.get("note") or ("机器人充值" if a["flowType"] == "1" else "机器人调整"))
    kind = "充值" if a["flowType"] == "1" else "调整"
    be.roster(force=True)
    bal = ""
    for st in be.roster():
        if str(st.get("id")) == str(a["studentId"]):
            acc = be.account_of(st, a.get("subject"))
            if acc:
                bal = "\n" + _bal_line(acc)
    dt = ("（记账日期 %s）" % a["occurDate"]) if a.get("occurDate") else ""
    return {"text": "✅ %s · %s 已%s：%s%s%s\n👉 %s"
                    % (a["studentName"], a.get("subject") or "", kind,
                       _flow_brief(a), dt, bal, h5("account")),
            "image_path": None}


# ---- ② 一键结算 ----

def settle_lines(r, n_req, exp_hours=None, exp_amount=None):
    """结算返回 → 回执行列表（⑧ 补录也复用这段，口径一处定义）。

    🔴 BE 契约实证（SettlementService.settle）：`settled` 是**结算成功的场次数（int）**，
    不是逐场明细数组。老代码按数组遍历它（sum/len/切片），真跑必 TypeError——
    确认闸之前一直用打桩 executor 回归，所以这个雷一直没炸。逐场金额只能靠预览侧算的
    期望值来报（exp_*），BE 这里不给。
    """
    ok = int(r.get("settled") or 0)
    skipped = r.get("skipped") or []
    head = "✅ 已结算 %d 场" % ok
    if ok == n_req and exp_hours is not None:
        head += "：共扣 %s 小时 / %s 元" % (money(exp_hours), money(exp_amount or 0))
    elif exp_hours is not None:
        head += "（原计划 %d 场，预估共扣 %s 小时；实际以下方跳过清单为准）" % (n_req, money(exp_hours))
    lines = [head]
    if skipped:
        lines.append("⚠️ 跳过 %d 场：" % len(skipped))
        for x in skipped[:8]:
            lines.append("  · 场次 %s —— %s" % (x.get("sessionId"), x.get("reason")))
    return lines


def _op_settle(be, a):
    items = a["items"]
    r = be.settle(items, gen_feedback=False) or {}
    lines = settle_lines(r, len(items), a.get("expectHours"), a.get("expectAmount"))
    lines.append("👉 %s" % h5("settle"))
    return {"text": "\n".join(lines), "image_path": None}


# ---- ③ 请假冲正 ----

def _op_leave(be, a):
    r = be.leave(a["sessionId"]) or {}
    h, amt = r.get("hours"), r.get("amount")
    if h or amt:
        head = "✅ %s 已置请假，并冲正已扣：退回 %s 小时 / %s 元" % (a["desc"], money(h), money(amt))
    else:
        head = "✅ %s 已置请假（该场未结算，无需冲正）" % a["desc"]
    tail = []
    if r.get("deletedShells"):
        tail.append("清理空反馈壳 %s 个" % r["deletedShells"])
    if r.get("keptShells"):
        tail.append("保留有内容反馈单 %s 个" % r["keptShells"])
    return {"text": head + ("\n（%s）" % "，".join(tail) if tail else "") + "\n👉 %s" % h5("schedule"),
            "image_path": None}


# ---- ④⑥ 写反馈 ----

def _op_feedback(be, a):
    bo = {"targetId": str(a["targetId"]), "rows": a["rows"], "lessonDate": a["lessonDate"]}
    for k in ("planId", "sessionId", "batchKey", "title"):
        if a.get(k) is not None:
            bo[k] = str(a[k]) if k in ("planId", "sessionId") else a[k]
    if a.get("lessonSeq") is not None:
        bo["lessonSeq"] = a["lessonSeq"]
    r = be.upsert_sheet(bo, a.get("sheetId")) or {}
    sheet_id = r.get("id") or a.get("sheetId")
    lines = ["✅ %s %s 的反馈单已保存（%d 行，单号 %s）"
             % (a["targetName"], a["lessonDate"], len(a["rows"]), sheet_id)]
    img = None
    try:
        if a.get("planId"):
            ex = be.export_plan_png(a["planId"], "single")
        else:
            ex = be.export_sheet_png(sheet_id)
        img = (ex or {}).get("url")
        lines.append("🖼 家长版图已生成，随后发出。")
    except BeError as e:
        lines.append("⚠️ 出图失败（反馈单已存）：%s" % e)
    lines.append("👉 %s" % h5("feedback"))
    return {"text": "\n".join(lines), "image_path": img}


# ---- ⑦ 家庭归账对倒：PRD-018 v3 退役（学生绑账本 n:1，两户已并一本共享账，无「对倒」概念） ----

def _op_family_rebalance(be, a):
    return {"text": "ℹ️ 两个孩子的课时已并成一本共享账（PRD-018），不存在两户对倒了。\n"
                    "共享账本的余额与流水直接看台账：%s" % h5("account"),
            "image_path": None}


# ---- ⑧ 看图补录课时本（建场次 → 立刻逐场结算 → 充值笔，一个 op 里连续做完） ----


def _op_ingest_lesson_log(be, a):
    """把手写课时本的历史记录补进系统。args 由 jiaowu_bot 的预览段备好。

    🔴 为什么建场次和结算必须挤在同一个 op 里、中间不留窗口：
       /settle/pending = 「结束时间已过 + 已排 + 未结」。补录建的全是历史场次，
       建完的那一瞬间它们**全部涌进老师的待结算列表**。中间只要一断，
       他的待办就凭空多出十几条，比不补录还糟。所以这里不设第二道确认。

    🔴 顺序 = 建场次 → 逐场结算 → 充值笔（用户拍板口径）。
       副作用：充值在最后，账户余额会先被扣成负数再被充回来，
       台账里 hoursAfter 中途为负是正常的（系统本来就允许欠费），最终值正确。
    """
    items = a["items"]                      # [{date,start,end,sessionType,subject,externalTitle,note}]
    hours_by_key = a["hoursByKey"]          # {"YYYY-MM-DD HH:mm": 实扣小时}
    content_by_key = a.get("contentByKey") or {}   # {同上: 这节实际讲了什么}
    lines = []

    # ① 建场次
    r = be.session_batch(a["studentId"], items) or {}
    created = r.get("created") or []
    if not created:
        raise BeError("一条场次都没建成（BE 返回 created 为空，conflicts=%d）。没有落库，可重发。"
                      % len(r.get("conflicts") or []))
    lines.append("✅ 已补建 %d 场历史场次" % len(created))
    if len(created) != len(items):
        lines.append("⚠️ 提交 %d 条只建成 %d 条，缺的那几条请到 H5 手工补：%s"
                     % (len(items), len(created), h5("schedule")))

    # ② 立刻逐场结算（🔴 不留窗口；genFeedback=False，补录不建反馈壳）
    st_items, exp_h = [], 0.0
    for s in created:
        key = "%s %s" % (s.get("sessionDate"), str(s.get("startTime"))[:5])
        hrs = float(hours_by_key.get(key) or 1.0)
        exp_h += hrs
        it = {"sessionId": str(s.get("id")), "hours": hrs}
        # 🔴 PRD-018 D5/G7：结算顺手写 session.content，台账「内容」列就不再是清一色「正课」
        if content_by_key.get(key):
            it["content"] = content_by_key[key]
        st_items.append(it)
    try:
        sr = be.settle(st_items, gen_feedback=False) or {}
    except BeError as e:
        # 半单：场次已建、结算没跑 → 老师的待结算列表会突然多出一堆，必须喊出来
        raise BeError(
            "⚠️ 补录只完成一半！%d 场历史场次**已经建好**，但批量结算失败（%s）。"
            "这些场次现在全躺在你的「待结算」里。请发一句「把待办清了」补结算，"
            "或上 H5 处理：%s" % (len(created), e, h5("settle")))
    lines += settle_lines(sr, len(st_items), exp_h, a.get("expectAmount"))
    n_skip = len(sr.get("skipped") or [])
    if n_skip:
        lines.append("⚠️ 上面跳过的场次已经建在系统里但没扣课时（含未开户的：已标已上待补扣），正躺在「待结算」，"
                     "发「把待办清了」可以补结。")

    # ③ 充值笔（PRD-018：occur_date=业务日期列已就位，补录充值直接回填真实日期）
    ok_rc, bad_rc = 0, []
    for rc in (a.get("recharges") or []):
        try:
            be.add_flow(a["accountId"], "1", hours=rc.get("hours") or 0,
                        amount=rc.get("amount") or 0, lessons=rc.get("lessons") or 0,
                        occur_date=rc.get("occurDate") or rc.get("date"),
                        note=rc.get("note") or "补录充值")
            ok_rc += 1
        except BeError as e:
            bad_rc.append("%s（%s）" % (rc.get("note") or "充值", e))
    if ok_rc:
        lines.append("✅ 已补 %d 笔充值（台账日期=真实业务日期）" % ok_rc)
    if bad_rc:
        lines.append("⚠️ 有 %d 笔充值没进去，请到 H5 手工补：%s\n   · %s"
                     % (len(bad_rc), h5("account"), "\n   · ".join(bad_rc)))

    # ④ 回执：拉最新余额 + 出流水单 PNG 给他核对
    be.roster(force=True)
    for st in be.roster():
        if str(st.get("id")) == str(a["studentId"]):
            acc = be.account_of(st, a.get("subject"))
            if acc:
                lines.append("📊 %s 当前余额：%s" % (a["studentName"], bal_text(acc)))
    img = None
    try:
        img = (be.export_ledger_png(a["accountId"]) or {}).get("url")
        lines.append("🖼 课时流水单已生成，随后发出，请对着课时本核一遍。")
    except BeError as e:
        lines.append("⚠️ 流水单出图失败（数据已落库）：%s" % e)
    lines.append("👉 %s" % h5("account"))
    return {"text": "\n".join(lines), "image_path": img}


_OPS = {
    "account_open": _op_account_open,
    "account_flow": _op_account_flow,
    "settle": _op_settle,
    "leave": _op_leave,
    "feedback": _op_feedback,
    "family_rebalance": _op_family_rebalance,
    "ingest_lesson_log": _op_ingest_lesson_log,
}
