"""MCP 工具·每日打卡组（PRD-013 批1，封 book-server /teacher/punch）。

打卡书 = 有结构的书（D1）：一本 `biz_shelf_book`（bookType=daily_punch），
一天 = 一个 `punch_day` 节点，一个模块 = 一个 item —— 生成器产完一天内容，
一条 `upsert_punch_day` 就进系统，人眼审核走 `submit_punch_review` 闭环。

四工具（agent 侧录入 + 回读 + 审核；FE 展示/就地点改走 BE 薄端点不经本模块）：
  upsert_punch_day    幂等建/覆盖「第 N 天」节点 + 各模块 item（同 book+day 重调=覆盖不重复建）
  list_punch_days     全书天目录 + 审核态（审核页目录角标 ✓/⚠/○ 的数据源）
  get_punch_day       读回一天完整内容（展示页/生成器回读共用）
  submit_punch_review 提交人眼审核结论（pass/issue/reopen），全天 pass 后置书级审定标记

设计对齐（与 oralcalc.py / shelf.py 同构）：
  - register(mcp, client) 注入单 RuoyiClient；工具先 client.has_session() 守卫。
  - tags={"prep"}：随备课角色暴露（打卡资料生产属备课/资料线）。
  - RuoyiError 一律捕获返 {ok:false, reason}，不抛给模型。

🔴 铁律：
  1. **所有 id 字符串传递**（book_id/node_id/item_id/qid）—— 雪花号走 JSON double 会截尾成死 id。
  2. **写工具先 login**（未登录直接返 {ok:false, reason:"需先 login"}）。
  3. **题库题永不复制题面文本** —— 轮换位只传 qid 字符串数组，题面由 BE 按 block_json 渲染
     （改题库即改打卡书，零漂移；题面唯一源 = block_json，D11）。

对接 BE（PunchController，A 线；契约已冻结）：
  GET  /teacher/punch/days?bookId=          → [{day,nodeId,itemCount,review}]
  GET  /teacher/punch/day?bookId=&day=      → {goals,modules,review}
  POST /teacher/punch/upsertDay             {bookId,day,goals:[str],modules:[...]} → {nodeId,itemIds}
  POST /teacher/punch/review                {bookId,day,action,issues?}            → {review}
"""
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/punch"

ACTIONS = ("pass", "issue", "reopen")


def _sid(v) -> Optional[str]:
    """id 一律出字符串（None 保持 None）—— 雪花号绝不以数字回传给模型。"""
    return None if v is None else str(v)


def _day_row(r: dict) -> dict:
    """BE 天目录行（camelCase）→ 工具口径（snake_case + id 转字符串）。"""
    return {
        "day": r.get("day"),
        "node_id": _sid(r.get("nodeId") if "nodeId" in r else r.get("node_id")),
        "item_count": r.get("itemCount") if "itemCount" in r else r.get("item_count"),
        "review": r.get("review") or {},
    }


def register(mcp, client: RuoyiClient) -> None:

    # ═════════════ 1. upsert_punch_day：整天灌入（幂等覆盖） ═════════════
    @mcp.tool(tags={"prep"})
    async def upsert_punch_day(book_id: str, day: int, goals: list[str],
                               modules: list[dict]) -> dict:
        """把「第 N 天」整天内容灌进打卡书（幂等：同 book+day 重调 = 覆盖，不重复建节点）。

        天节点名由 BE 按 day 生成，goals 落节点 meta_json.goals（今日目标条，简短口径，
        🔴 学生/家长可见 → 禁内部词：层/★/素材/薄弱/挑题）。modules **原样透传**给 BE。

        参数:
          book_id : 打卡书 id（🔴 字符串；list_books(book_type='daily_punch') 查，严禁编造）
          day     : 第几天（1 起正整数；同一 day 重灌 = 整天覆盖，agent 侧批量修正走这条路）
          goals   : 今日目标 ["乘法连续进位","小数退位减","年、月、日"]（简短短语，非整句）
          modules : 模块数组，按卷面顺序排；两类结构 ——
            ① 计算模块（出题器现产，题目不在题库 → 内容随书存 content_json）：
               {"type":"oral|vertical|stepwise", "title":"口算题",
                "items":[{"q":"357＋276＝","a":"633"}, ...]}
               type 三型 = oral 口算 / vertical 竖式 / stepwise 脱式；q 题面、a 答案**成对**给全
               （答案缺失 = 解析卷开天窗）。题目由 generate_calc_items 产，无需再人工验算。
            ② 轮换位（教辅真题，题在题库 → **引用不复制**）：
               {"type":"rotating", "title":"解决问题", "qids":["2077057695340310530", ...]}
               🔴 qids = **题目 id 的字符串数组**（雪花号，search_questions / 生成器给的原样字符串）；
               🔴 **绝不传题面文本**——题面由 BE 按 biz_question_block.block_json 渲染，
                  改题库即改打卡书，零漂移（D5/D11）。传文本 = 制造第二个半源，必被打回。

        返回: {ok, node_id(str), item_ids:[str]}；未登录/参数不合法/BE 报错 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id or "").strip():
            return {"ok": False, "reason": "book_id 不能为空（字符串传，list_books 查）"}
        if not isinstance(day, int) or day < 1:
            return {"ok": False, "reason": f"day 需 ≥1 的整数，收到 {day!r}"}
        if not modules:
            return {"ok": False, "reason": "modules 不能为空：[{type,title,items|qids}]"}
        body = {
            "bookId": str(book_id),          # 🔴 str 防雪花截尾
            "day": day,
            "goals": list(goals or []),
            "modules": modules,              # 🔴 原样透传，不在 MCP 侧改写结构
        }
        try:
            r = await client.teacher_post(f"{BASE}/upsertDay", body) or {}
        except RuoyiError as e:
            return {"ok": False, "reason": f"灌入第{day}天失败: {e}"}
        if not isinstance(r, dict):
            return {"ok": False, "reason": f"upsertDay 返回异常: {str(r)[:200]}"}
        return {
            "ok": True,
            "node_id": _sid(r.get("nodeId")),
            "item_ids": [str(x) for x in (r.get("itemIds") or [])],
        }

    # ═════════════ 2. list_punch_days：天目录 + 审核态 ═════════════
    @mcp.tool(tags={"prep"})
    async def list_punch_days(book_id: str) -> dict:
        """列一本打卡书的全部天（目录 + 审核态）→ {ok, days:[{day,node_id,item_count,review}]}。

        review = {status:"pending|passed|issue", issueCount}（审核页目录角标 ○/✓/⚠N 的数据源）。
        🔴 重灌某天前先用它看该天是否已存在、审核到哪一步（已 passed 的天重灌会把审核态打回）。
        参数 book_id 字符串传。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id or "").strip():
            return {"ok": False, "reason": "book_id 不能为空（字符串传，list_books 查）"}
        try:
            r = await client.teacher_get(f"{BASE}/days", {"bookId": str(book_id)})
        except RuoyiError as e:
            return {"ok": False, "reason": f"取天目录失败: {e}"}
        rows = r.get("days") or r.get("rows") or [] if isinstance(r, dict) else (r or [])
        days = [_day_row(x) for x in rows if isinstance(x, dict)]
        return {"ok": True, "days": days, "total": len(days)}

    # ═════════════ 3. get_punch_day：读回一天完整内容 ═════════════
    @mcp.tool(tags={"prep"})
    async def get_punch_day(book_id: str, day: int) -> dict:
        """读回「第 N 天」完整内容 → {ok, goals, modules, review}（展示页/生成器回读共用）。

        modules 结构与 upsert_punch_day 入参同构：计算模块带 items:[{q,a}]；
        轮换位为**引用渲染态**（BE 按 qid 组装 blocks，题面来自题库 block_json，不是复制文本）。
        review = {status, issues:[{module,seq,kind,note,resolved}]}。
        🔴 局部改一天内容 = 先 get 拿全量 → 改 → 整天 upsert_punch_day 回灌（覆盖语义）。
        参数: book_id 字符串；day 第几天（1 起）。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id or "").strip():
            return {"ok": False, "reason": "book_id 不能为空（字符串传，list_books 查）"}
        if not isinstance(day, int) or day < 1:
            return {"ok": False, "reason": f"day 需 ≥1 的整数，收到 {day!r}"}
        try:
            r = await client.teacher_get(f"{BASE}/day", {"bookId": str(book_id), "day": day})
        except RuoyiError as e:
            return {"ok": False, "reason": f"取第{day}天失败: {e}"}
        if not isinstance(r, dict):
            return {"ok": False, "reason": f"day 返回异常: {str(r)[:200]}"}
        return {
            "ok": True,
            "day": day,
            "node_id": _sid(r.get("nodeId")),
            "goals": r.get("goals") or [],
            "modules": r.get("modules") or [],
            "review": r.get("review") or {},
        }

    # ═════════════ 4. submit_punch_review：人眼审核结论 ═════════════
    @mcp.tool(tags={"prep"})
    async def submit_punch_review(book_id: str, day: int, action: str,
                                  issues: Optional[list[dict]] = None) -> dict:
        """提交「第 N 天」人眼审核结论（审核流主闭环）→ {ok, review}。

        action 三态:
          pass   本天通过（🔴 全书每一天都 pass 后，BE 自动置**书级审定标记**——书由此可挂 SKU 上架）
          issue  本天记问题（必带 issues；天角标转 ⚠N，进全书问题清单）
          reopen 销账后重开审核（问题已修复重灌 → 打回 pending 再审一遍）

        参数:
          book_id: 打卡书 id（字符串）
          day    : 第几天（1 起）
          action : "pass" | "issue" | "reopen"（其余值直接拒，不猜）
          issues : action=issue 时必带 —— [{module, seq, kind, note}]
            module = 模块 type（oral/vertical/stepwise/rotating）
            seq    = 模块内题号（1 起）
            kind   = "难度不符" | "题面有误" | "排版" | "其他"
            note   = 问题描述（一句话说清改什么，供重产/重灌时对账）
        🔴 大改（换题/调难度）= 记问题 → 重产 → upsert_punch_day 重灌 → reopen 重审；
           打卡页对题库题只读，改题库题走题目详情页修改态（D10/D11），不在本工具面内。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not str(book_id or "").strip():
            return {"ok": False, "reason": "book_id 不能为空（字符串传，list_books 查）"}
        if not isinstance(day, int) or day < 1:
            return {"ok": False, "reason": f"day 需 ≥1 的整数，收到 {day!r}"}
        act = (action or "").strip().lower()
        if act not in ACTIONS:
            return {"ok": False, "reason": f"未知 action: {action!r}（pass|issue|reopen）"}
        if act == "issue" and not issues:
            return {"ok": False, "reason": "action=issue 必带 issues:[{module,seq,kind,note}]"}
        body: dict = {"bookId": str(book_id), "day": day, "action": act}
        if issues:
            body["issues"] = issues
        try:
            r = await client.teacher_post(f"{BASE}/review", body) or {}
        except RuoyiError as e:
            return {"ok": False, "reason": f"提交审核({act})失败: {e}"}
        if not isinstance(r, dict):
            return {"ok": False, "reason": f"review 返回异常: {str(r)[:200]}"}
        out = {"ok": True, "day": day, "action": act, "review": r.get("review") or r}
        if "bookAudited" in r:                      # BE 全 pass 时回传书级审定标记
            out["book_audited"] = r.get("bookAudited")
        return out
