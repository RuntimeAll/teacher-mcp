"""MCP 工具·共享层：login / list_kg_tree / resolve_kg / search_questions / get_question /
get_role_manual + health_check。全角色可见（tags={"shared"}）。

PRD-O-005 重建：单 client（A/C 已合并 :9090）；题库读侧（search/get）从旧 qbank.py 平移进本文件。
get_role_manual 改读 src/teacher_mcp/manuals/<role>.md（role 缺省跟随 TEACHER_MCP_ROLE）。
"""
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from teacher_mcp.config import settings
from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

_MANUAL_DIR = Path(__file__).resolve().parent.parent / "manuals"


# ───────────────────────── 说明书：role → 文件名 ─────────────────────────
def _manual_file(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("all", "总", "全", "总手册"):
        return "all"
    if r in ("prep", "备课", "prep-role", "lesson"):
        return "prep"
    if r in ("variant", "变式", "举一反三"):
        return "variant"
    return "data"  # ingest / lecture / data / 其余一律录入手册


def _read_manual(fname: str) -> Optional[str]:
    p = _MANUAL_DIR / f"{fname}.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


# ───────────────────────── 题库读侧辅助（平移自旧 qbank.py）─────────────────────────
def _rows(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("list", "rows", "records", "items"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def _total(resp) -> int:
    if isinstance(resp, dict):
        t = resp.get("total")
        if t is not None:
            try:
                return int(t)
            except (TypeError, ValueError):
                return 0
    return len(_rows(resp))


def _brief(s: Optional[str], n: int = 120) -> str:
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def _map_item(it: dict) -> dict:
    return {
        "id": str(it.get("id")) if it.get("id") is not None else None,
        "question_type": it.get("questionType"),
        "difficult": it.get("difficult"),
        "subject_id": it.get("subjectId"),
        "stem_brief": _brief(it.get("stemText")),
        "stem_img": it.get("stemImg"),
        "status": it.get("status"),
        "label_status": it.get("labelStatus"),
        "free_tags": [t.get("name") for t in (it.get("freeTags") or []) if isinstance(t, dict)],
        "patterns": [p.get("name") for p in (it.get("patterns") or []) if isinstance(p, dict)],
        "exam_paper_name": it.get("examPaperName"),
        "create_user": it.get("createUser"),
    }


def _map_detail(it: dict) -> dict:
    return {
        "id": str(it.get("id")) if it.get("id") is not None else None,
        "question_type": it.get("questionType"),
        "difficult": it.get("difficult"),
        "subject_id": it.get("subjectId"),
        "stem_text": it.get("stemText"),
        "options": None,
        "answer": it.get("answer"),
        "analyze": it.get("explain"),
        "stem_img": it.get("stemImg"),
        "answer_img": it.get("answerImg"),
        "explain_img": it.get("explainImg"),
        "block_json": it.get("blockJson"),
        "answer_block_json": it.get("answerBlockJson"),
        "analyze_block_json": it.get("analyzeBlockJson"),
        "label_status": it.get("labelStatus"),
        "status": it.get("status"),
        "free_tags": [t.get("name") for t in (it.get("freeTags") or []) if isinstance(t, dict)],
        "patterns": [p.get("name") for p in (it.get("patterns") or []) if isinstance(p, dict)],
        "question_knowledges": it.get("questionKnowledges"),
        "create_user": it.get("createUser"),
    }


def _as_long(v):
    if v is None:
        return None
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


# ───────────────────────── 溯源找回（PRD-O-005）辅助 ─────────────────────────
def _parse_since(s: str):
    """解析 since：'24h'/'7d'（相对当下）或 ISO 日期（'2026-07-08' / '2026-07-08 12:00:00'）。
    返回 datetime cutoff；无法解析 → None。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([hHdD])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return datetime.now() - (timedelta(hours=n) if unit == "h" else timedelta(days=n))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _map_recall_item(row: dict) -> dict:
    """DB 找回行 → 与 _map_item 同 shape（stem NULL 的变式题用占位不丢行）+ 附溯源三键。"""
    d = dict(row)
    if not d.get("stemText"):
        d["stemText"] = "(富文本题面)"
    item = _map_item(d)
    item["import_source"] = row.get("importSource")
    item["batch_id"] = row.get("importBatchId")
    item["create_time"] = row.get("createTime")
    return item


async def _search_questions(client, subject_id="", question_type=None, difficult=None,
                            keyword="", mine=False, exam_paper_id=None, label_status=None,
                            pattern_id=None, page_index=1, page_size=20) -> dict:
    body: dict = {"pageIndex": int(page_index), "pageSize": int(page_size)}
    if subject_id:
        body["subjectId"] = str(subject_id)
    if question_type is not None:
        body["questionType"] = int(question_type)
    if difficult is not None:
        body["difficult"] = int(difficult)
    if keyword:
        body["keyWord"] = keyword
    if mine:
        body["mine"] = True
    epi = _as_long(exam_paper_id)
    if epi is not None:
        body["examPaperId"] = epi
    if label_status is not None:
        body["labelStatus"] = int(label_status)
    pid = _as_long(pattern_id)
    if pid is not None:
        body["patternId"] = pid
    resp = await client.teacher_post("/teacher/question/page", body)
    rows = _rows(resp)
    return {"ok": True, "total": _total(resp), "items": [_map_item(r) for r in rows]}


async def _get_question(client, ids) -> dict:
    id_list = [str(i) for i in (ids or []) if str(i).strip()]
    if not id_list:
        return {"ok": False, "error": "ids 为空"}
    if len(id_list) > 100:
        return {"ok": False, "error": f"单次最多 100 题（收到 {len(id_list)}）"}
    resp = await client.teacher_get("/teacher/question/list", {"ids": ",".join(id_list)})
    rows = resp if isinstance(resp, list) else _rows(resp)
    return {"ok": True, "items": [_map_detail(r) for r in rows if isinstance(r, dict)]}


# ───────────────────────── 注册 ─────────────────────────
def register(mcp, client: RuoyiClient, default_role: str = "data",
             hide_auth_tools: bool = False) -> None:
    """注册共享工具组。

    🔴 hide_auth_tools（PRD-007 后端锁身份）：True → 不注册 login / login_as 两工具，使模型无法自行切身份
       （防提示注入：用户消息里写「用 admin 身份」也无从执行，身份由后端 BOUND_OPENID 环境变量锁死）。
       build_server 据 settings.bound_openid 非空传 True。False（默认）→ 两工具照常注册，现有行为不变。
    """
    # ── 说明书 resource（三角色各一，全角色可见）──
    @mcp.resource("teacher://manual/data")
    def _manual_data() -> str:
        return _read_manual("data") or "（录入角色说明书缺失）"

    @mcp.resource("teacher://manual/prep")
    def _manual_prep() -> str:
        return _read_manual("prep") or "（备课角色说明书缺失）"

    @mcp.resource("teacher://manual/variant")
    def _manual_variant() -> str:
        return _read_manual("variant") or "（变式角色说明书缺失）"

    @mcp.resource("teacher://manual/all")
    def _manual_all() -> str:
        return _read_manual("all") or "（总手册缺失）"

    async def login(username: str = "", password: str = "") -> dict:
        """以真实 teacher 账号登录平台，拿双头 token 注入本会话身份（后续所有工具隐式带该身份、落 RuoYi 权限审计）。

        参数:
          username/password: teacher 账号。留空则用 .env 兜底（RUOYI_USERNAME/RUOYI_PASSWORD）。
        返回:
          {ok, teacher_id, username} —— token 仅驻留 server 侧会话态，不回吐明文给调用方。
        """
        u = username or settings.ruoyi_username
        p = password or settings.ruoyi_password
        try:
            info = await client.login(u, p)
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        return {"ok": True, "teacher_id": info["user_id"], "username": info["username"]}

    async def login_as(openid: str) -> dict:
        """飞书机器人免密切身份（PRD-007）：凭服务密钥用 open_id 换该 teacher 的 token，替换本会话身份。

        用途：bot 后端按飞书消息发送者的 open_id 逐消息切身份，让写操作（录题/组卷等）归属各自 teacher。
        鉴权：服务密钥走 env BOT_SECRET（只在机器人后端持有、不入 git）；调用方不需密码。
        401 自动重签：切身份后，后续工具遇 token 失效会按当前 open_id 自动重调 botLogin 重签（不走密码重登）。
        参数:
          openid: 飞书 open_id（如 ou_xxx）。
        返回:
          成功 → {ok:true, user_id, openid}。
          失败 → {ok:false, hint}：openid 未绑定 teacher → hint 含「未绑定」（bot 按此路由拒绝话术）；
                 BOT_SECRET 未配置 / 密钥错 / 账号停用等各有对应提示。
        """
        if not openid or not str(openid).strip():
            return {"ok": False, "hint": "openid 为空：请传飞书消息发送者的 open_id"}
        if not settings.bot_secret:
            return {"ok": False, "hint": "BOT_SECRET 未配置：请在机器人后端 .env 配置服务密钥 BOT_SECRET（不入 git）"}
        try:
            info = await client.login_as(str(openid).strip())
        except RuoyiError as e:
            return {"ok": False, "hint": str(e)}
        return {"ok": True, "user_id": info["user_id"], "openid": info["openid"]}

    # 🔴 PRD-007 后端锁身份：仅当未隐藏时才把 login / login_as 暴露为工具。
    #    bound_openid 非空 → hide_auth_tools=True → 两工具不注册，模型无法自行切身份（防提示注入）。
    if not hide_auth_tools:
        mcp.tool(tags={"shared"})(login)
        mcp.tool(tags={"shared"})(login_as)

    @mcp.tool(tags={"shared"})
    async def list_kg_tree() -> dict:
        """查平台知识点树（组卷的知识点白名单源）。返回顶层节点 + children 嵌套。

        编排层（Claude Code）据此选要考查的知识点叶子 id，喂给 compose_paper 的 outline.subjectId。
        返回: {ok, nodes:[{id,name,children?}, ...]}；空树 → {ok:true, nodes:[]} 不报错。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            tree = await client.lazy_tree({})
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
        if isinstance(tree, dict):
            tree = tree.get("rows") or tree.get("nodes") or []
        if not isinstance(tree, list):
            tree = []
        return {"ok": True, "nodes": tree}

    @mcp.tool(tags={"shared"})
    def resolve_kg(
        subject_root: str,
        query: str = "",
        section_num: str = "",
        parent_id: str = "",
        leaves_only: bool = False,
        limit: int = 50,
    ) -> dict:
        """KG 锚定查表（确定性只读，数学+科学一套通吃）：按名称/节号/父节点在某教材根下查节点，供选锚定叶子。

        🔴 叶子=无子节点（is_leaf），别按 level 判——科学 901 树 5 层、902-906 树 4 层，叶深不一（H2 实测）。
        用法：先 query 模糊（如「乘方」）看候选 → 挑 is_leaf=true 的最贴切者作 ingest_items 的 kp_id；
        同步练习类卷名带节号可 section_num 精确命中（如 "2.5" → 「2.5 有理数的乘方」节点）。
        参数:
          subject_root: 教材根 id（数学七上="100"；科学="901".."906"）——锚定范围的唯一开关
          query       : 名称模糊词（LIKE %query%）
          section_num : 节号精确匹配（如 "2.5"，命中名称以「2.5 」开头的节点）
          parent_id   : 只列某节点的直接子节点（浏览下钻用；给了它则忽略 subject_root 前缀过滤）
          leaves_only : 只返回叶子
        返回: {ok, count, nodes:[{id,name,level,parent_id,is_leaf}]}；无命中 → count=0 不报错。
        """
        if not subject_root and not parent_id:
            return {"ok": False, "reason": "subject_root 必填（数学七上=100 / 科学=901..906）"}
        from teacher_mcp.backends import db
        try:
            nodes = db.kg_query(subject_root, query=query, section_num=section_num,
                                parent_id=parent_id, leaves_only=leaves_only, limit=limit)
        except Exception as e:
            return {"ok": False, "reason": f"KG 查表失败: {type(e).__name__}: {e}"}
        return {"ok": True, "count": len(nodes), "nodes": nodes}

    @mcp.tool(tags={"shared"})
    async def search_questions(
        subject_id: str = "", question_type: int = None, difficult: int = None,
        keyword: str = "", mine: bool = False, exam_paper_id: str = None,
        label_status: int = None, pattern_id: str = None,
        batch_id: str = "", since: str = "",
        page_index: int = 1, page_size: int = 20,
    ) -> dict:
        """从题库分页检索题目（备课圈题核心）→ POST /teacher/question/page。返回 {ok, total, items}。

        🔴 快速找回路径（PRD-O-005 溯源增强）：给 batch_id 或 since 任一 → 改走 backends/db 只读检索
           （按 import_batch_id / create_time / create_user 查，**不依赖 stem LIKE**，故不漏 stem_text=NULL
           的变式题），可与 mine/subject_id/question_type/difficult 组合。返回 items 附 import_source/batch_id/create_time。
           - batch_id: 精确批次号（ingest_items/ingest_question 返回的 batch_id，如 mcp-20260708-...）
           - since   : 时间窗，'24h'/'7d' 或 ISO 日期（'2026-07-08'）；mine=True 限本人。

        🔴 subject_id 语义 = **前缀子树匹配**：传章节/课时的 biz_subject 节点 id → 召回该节点下
           **整棵子树**所有知识点的题（likeRight 前缀）；传叶子 id → 只该叶子的题。非数字 id → BE 静默返空集。
        🔴 私有 vs 公共两套口径：
           - 默认（mine=False）= 公共池（status='1' AND is_public=1，仅超管审核过的题）；
           - mine=True = **本人已发布题**（create_user=登录老师，status='1'，含 is_public=0 私有题、
             含举一反三变式/据讲义自造题）。🔴 圈自己造的题必须 mine=True。
           - 🔴 两种口径都只返 status='1'（已发布）——草稿 status='0' 永不进列表。
        参数:
          subject_id   : 知识点/章节/课次的 biz_subject id（resolve_kg / get_plan_detail.kgNodeIds 来）；空=不过滤
          question_type: 题型码 1选择/2判断/3应用/4填空/5解答/6作图/7计算/8证明；None=不限
          difficult    : 难度 1-4 星（按段分层规则挑档）；None=不限
          keyword      : 题干 LIKE %kw%
          mine         : True=只看本人已发布题（含私有池）；False=公共池
          exam_paper_id: 按出处卷 id 筛（字符串数字）；None=不限
          label_status : 打标态 0未标/1AI已标/2已审核；None=不限
          pattern_id   : 题型 id（biz_question_pattern）收窄；None=不限
          page_index   : 页码（🔴 从 1 起，非 0）
          page_size    : 每页条数（默认 20）
        返回: {ok, total, items:[{id(str 雪花), question_type, difficult, subject_id, stem_brief,
              stem_img, status, label_status, free_tags, patterns, ...}]}。id 直接透给 build_prep_pack。
        """
        if not client.has_session():
            return {"ok": False, "error": "需先 login"}
        # 🔴 后端锁身份下 has_session 靠 bound_openid 放行，真正的 token/user_id 在此惰性建立（DB 找回路径要 user_id）
        await client.ensure_session()
        # ── 快速找回路径：batch_id / since 任一给出 → DB 只读检索（绕 stem LIKE，不漏变式题）──
        if batch_id or since:
            since_dt = None
            if since:
                since_dt = _parse_since(since)
                if since_dt is None:
                    return {"ok": False, "error": f"since 无法解析：{since}（用 24h/7d 或 ISO 日期 2026-07-08）"}
            from teacher_mcp.backends import db
            try:
                rows = db.recent_questions(
                    uid=client.user_id, batch_id=batch_id, since_dt=since_dt, mine=mine,
                    subject_id=subject_id, question_type=question_type, difficult=difficult,
                    limit=int(page_size), offset=(int(page_index) - 1) * int(page_size))
            except Exception as e:
                return {"ok": False, "error": f"找回检索失败: {type(e).__name__}: {e}"}
            items = [_map_recall_item(r) for r in rows]
            return {"ok": True, "total": len(items), "items": items, "recall": True}
        try:
            return await _search_questions(client, subject_id, question_type, difficult, keyword,
                                           mine, exam_paper_id, label_status, pattern_id,
                                           page_index, page_size)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"shared"})
    async def my_recent_uploads(hours: int = 24) -> dict:
        """一键找回当前登录老师在最近时间窗内录的东西（题 / 卷 / 讲义片段）——DB 只读，不依赖 stem 关键词。

        用途：老师刚用 MCP 录完一批题/组完卷/存完讲义，想快速核对「我刚才录进去了啥」。
        参数: hours 时间窗（默认 24 小时；<1 视为 1）。
        返回:
          {ok, hours,
           questions: {total, batches:[{batch_id, import_source, count, items:[{id, stem_head(30字/占位),
                       import_source, batch_id, create_time}]}]}   # 按录入批次分组，倒序
           papers: [{id, name, question_count, create_time}]        # biz_paper 同窗口本人建的卷
           lecture_frags: [{id, title, create_time}]                # biz_kg_lecture_frag 同窗口本人 owner 的片段
           view_url}                                                # 题库页深链，供浏览器核对
        🔴 双管道语义：import_source 带 "mcp-" 前缀 = MCP 机录；'举一反三'=引擎落库；其余=手工/其他管道。
        """
        if not client.has_session():
            return {"ok": False, "error": "需先 login"}
        # 🔴 后端锁身份下 has_session 靠 bound_openid 放行，真正的 token/user_id 在此惰性建立
        await client.ensure_session()
        uid = client.user_id
        if uid is None:
            return {"ok": False, "error": "会话无 user_id，请重新 login"}
        cutoff = datetime.now() - timedelta(hours=max(1, int(hours)))
        from teacher_mcp.backends import db
        try:
            qrows = db.recent_questions(uid=uid, since_dt=cutoff, mine=True, limit=500)
            papers = db.recent_papers(uid, cutoff)
            frags = db.recent_lecture_frags(uid, cutoff)
        except Exception as e:
            return {"ok": False, "error": f"找回失败: {type(e).__name__}: {e}"}
        # 题按批次分组（保留首见顺序 = 时间倒序）
        batches: dict = {}
        for r in qrows:
            bkey = r.get("importBatchId") or "(无批次)"
            g = batches.get(bkey)
            if g is None:
                g = {"batch_id": r.get("importBatchId"), "import_source": r.get("importSource"),
                     "count": 0, "items": []}
                batches[bkey] = g
            g["count"] += 1
            head = (r.get("stemText") or "")[:30] or "(富文本题面)"
            g["items"].append({
                "id": r.get("id"), "stem_head": head, "import_source": r.get("importSource"),
                "batch_id": r.get("importBatchId"), "create_time": r.get("createTime"),
            })
        return {
            "ok": True,
            "hours": hours,
            "questions": {"total": len(qrows), "batches": list(batches.values())},
            "papers": papers,
            "lecture_frags": frags,
            "view_url": "http://localhost:9091/question/index",
        }

    @mcp.tool(tags={"shared"})
    async def get_question(ids: list) -> dict:
        """按 id 批量拉题目详情（🔴 装段前人工核对题面/答案/解析，避免盲装误人子弟）→
        GET /teacher/question/list?ids=。返回 {ok, items}。

        参数:
          ids : 题 id 列表（字符串雪花号，单次 ≤100）；软删题 BE 自动过滤，返回按入参保序。
        返回: {ok, items:[{id, stem_text, answer, analyze, question_type, difficult, subject_id,
              block_json/answer_block_json/analyze_block_json（三端结构化渲染源）, stem_img,
              free_tags, patterns, question_knowledges（U 轨 kp）, status, label_status}]}。
              🔴 选项内容在 block_json 里（选择题），无独立 options 字段。
        """
        if not client.has_session():
            return {"ok": False, "error": "需先 login"}
        try:
            return await _get_question(client, ids)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"shared"})
    def get_role_manual(role: str = "") -> dict:
        """取角色说明书全文（协议内自带；另有 MCP resource teacher://manual/<role>）。

        role 分角色返回（缺省跟随本连接的 TEACHER_MCP_ROLE）：
          - role="data"/"ingest"/"lecture"（录入线）= 录入角色说明书（七类来源路由 + IngestItem 契约 + 讲义录入）。
          - role="prep" = 备课角色说明书（备课线路编排 + 私有池铁律 + 变式补题路径）。
          - role="variant" = 举一反三角色说明书（批 3 落笔）。
          - role="all" = 总手册（开场三步 + 四线工具地图 + 四条编排流程 + 铁律盒子 + 自救表）——fresh agent 首选。
        🔴 首次以某身份使用本 server 的 agent 先调对应 role：说明书告诉你这条线怎么一步步走、每步调什么工具。
        返回: {ok, role, manual}（markdown 全文）；文件缺失 → {ok:False, hint}。
        """
        fname = _manual_file(role or default_role)
        text = _read_manual(fname)
        if text is None:
            return {"ok": False, "hint": f"说明书 manuals/{fname}.md 不存在（该角色手册尚未落笔）"}
        return {"ok": True, "role": fname, "manual": text}

    @mcp.tool(tags={"shared"})
    async def health_check() -> dict:
        """探活三依赖（ruoyi BE / toolkit / MySQL），任何异常算 down 不抛。返回 {ruoyi, toolkit, db}。

        - ruoyi: GET /actuator/health（401 也算 up，健康端点要鉴权属正常）
        - toolkit: GET /info
        - db: pymysql SELECT 1
        全程 trust_env=False、超时 5s。返回 {ruoyi:{up,url}, toolkit:{up,url}, db:{up}}。
        """
        ruoyi_url = settings.ruoyi_base_url
        toolkit_url = settings.toolkit_base_url
        ruoyi_up = toolkit_up = db_up = False
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as hc:
            try:
                r = await hc.get(f"{ruoyi_url.rstrip('/')}/actuator/health")
                ruoyi_up = r.status_code in (200, 401)  # 401 = BE 活着（健康端点要鉴权）
            except Exception:
                ruoyi_up = False
            try:
                r = await hc.get(f"{toolkit_url.rstrip('/')}/info")
                toolkit_up = r.status_code < 500
            except Exception:
                toolkit_up = False
        try:
            from teacher_mcp.backends import db as _db
            c = _db.conn()
            with c.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            c.close()
            db_up = True
        except Exception:
            db_up = False
        return {
            "ruoyi": {"up": ruoyi_up, "url": ruoyi_url},
            "toolkit": {"up": toolkit_up, "url": toolkit_url},
            "db": {"up": db_up},
        }
