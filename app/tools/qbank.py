"""MCP 工具·题库读侧（PRD-C-213 R7a·备课圈料）：search_questions / get_question。

🔴 全部打 **C 线 :8090**（挂 /teacher/question/**，misikt envelope）——register 收 cluster，
   每个 @mcp.tool() 先 `client = await cluster.ensure_c()`（懒登录 C 线），绝不用 cluster.a（那是 A 线 :8080）。
   题库读端点 /teacher/question/** 两个 clone 都在，本模块统一走 :8090，与备课包/讲义同会话免跨底座。

🔴 模块结构（照 schedule.py 范式）：核心逻辑 = 模块级 async 纯函数 `_xxx(client, ...)`（收已 ensure_c 的
   C 线 client）；register 里的 @mcp.tool() 只薄包一层（ensure_c + try/except RuoyiError → {ok:False}）。

🔴 id 全链路字符串（雪花 19 位，JSON number 会截尾）——返回的题 id 一律 str() 化，直接透给
   build_prep_pack 的 question_ids。

映射约定：snake_case 入参 → BE QuestionPageBo camelCase（🔴 pageIndex 非 pageNum / keyWord 驼峰 /
   difficult 非 difficulty）。
"""
from typing import Optional

from app.ruoyi import RuoyiCluster, RuoyiError

BASE = "/teacher/question"


def _rows(resp) -> list:
    """page 响应归一成 list：MisiktPageVo 的 {list} / 裸 list / {rows,records,items} 都吃。"""
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
    """题干缩略（列表卡够用；看全文走 get_question）。"""
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def _map_item(it: dict) -> dict:
    """QuestionItemVo → 圈题够用的精简投影（id str 化防截尾）。"""
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
    """QuestionDetailVo → 装段前核对够用的详情（保留题面/答案/解析全文 + 结构块 + U 轨 kp）。"""
    return {
        "id": str(it.get("id")) if it.get("id") is not None else None,
        "question_type": it.get("questionType"),
        "difficult": it.get("difficult"),
        "subject_id": it.get("subjectId"),
        "stem_text": it.get("stemText"),
        "options": None,  # 选项在 blockJson 里，装段无需拆；保留占位提示走 block_json
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
    """把 str/int id 归一成可给 BE Long 的 int；非数字返 None（BE 静默不过滤）。"""
    if v is None:
        return None
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


# ───────────────────────── 核心纯函数 ─────────────────────────
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
    resp = await client.teacher_post(f"{BASE}/page", body)
    rows = _rows(resp)
    return {"ok": True, "total": _total(resp), "items": [_map_item(r) for r in rows]}


async def _get_question(client, ids) -> dict:
    id_list = [str(i) for i in (ids or []) if str(i).strip()]
    if not id_list:
        return {"ok": False, "error": "ids 为空"}
    if len(id_list) > 100:
        return {"ok": False, "error": f"单次最多 100 题（收到 {len(id_list)}）"}
    resp = await client.teacher_get(f"{BASE}/list", {"ids": ",".join(id_list)})
    rows = resp if isinstance(resp, list) else _rows(resp)
    return {"ok": True, "items": [_map_detail(r) for r in rows if isinstance(r, dict)]}


# ───────────────────────── MCP 工具注册（薄包一层）─────────────────────────
def register(mcp, cluster: RuoyiCluster) -> None:
    @mcp.tool()
    async def search_questions(
        subject_id: str = "", question_type: int = None, difficult: int = None,
        keyword: str = "", mine: bool = False, exam_paper_id: str = None,
        label_status: int = None, pattern_id: str = None,
        page_index: int = 1, page_size: int = 20,
    ) -> dict:
        """从题库分页检索题目（备课圈题核心）→ 打 C 线 :8090 POST /teacher/question/page。返回 {ok, total, items}。

        🔴 subject_id 语义 = **前缀子树匹配**：传章节/课时的 biz_subject 节点 id → 召回该节点下
           **整棵子树**所有知识点的题（likeRight 前缀）；传叶子 id → 只该叶子的题。非数字 id → BE 静默返空集。
        🔴 私有 vs 公共两套口径：
           - 默认（mine=False）= 公共池（status='1' AND is_public=1，仅超管审核过的题）；
           - mine=True = **本人已发布题**（create_user=登录老师，status='1'，含 is_public=0 私有题、
             含举一反三变式/据讲义自造题）。🔴 圈自己造的题必须 mine=True。
           - 🔴 两种口径都只返 status='1'（已发布）——草稿 status='0' 永不进列表（见 get_role_manual 备课手册）。
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
        try:
            client = await cluster.ensure_c()
            return await _search_questions(client, subject_id, question_type, difficult, keyword,
                                           mine, exam_paper_id, label_status, pattern_id,
                                           page_index, page_size)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def get_question(ids: list) -> dict:
        """按 id 批量拉题目详情（🔴 装段前人工核对题面/答案/解析，避免盲装误人子弟）→ C 线 :8090
        GET /teacher/question/list?ids=。返回 {ok, items}。

        参数:
          ids : 题 id 列表（字符串雪花号，单次 ≤100）；软删题 BE 自动过滤，返回按入参保序。
        返回: {ok, items:[{id, stem_text, answer, analyze, question_type, difficult, subject_id,
              block_json/answer_block_json/analyze_block_json（三端结构化渲染源）, stem_img,
              free_tags, patterns, question_knowledges（U 轨 kp）, status, label_status}]}。
              🔴 选项内容在 block_json 里（选择题），无独立 options 字段。
        """
        try:
            client = await cluster.ensure_c()
            return await _get_question(client, ids)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
