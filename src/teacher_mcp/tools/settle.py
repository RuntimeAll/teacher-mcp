"""MCP 工具·课时结算组（PRD-015 批6，封 book-server /teacher/schedule/settle）。

教务一条线的第三段（排课 → 过点 → **结算** → 反馈）：
  list_pending_settlements  过点未结场次清单（纯读，只提醒不动账）
  settle_sessions           老师确认后一键结算 = 扣课时课费 + 场次标已上/已结 + 自动建反馈壳

🔴 **只提醒不自动扣（D4）**：系统永不自建扣费——本组是扣费的唯一入口，
   必须老师明确说「结算/消课/扣课时」才调 settle_sessions，绝不因为"看到有待结算"就自作主张扣。

🔴 **扣费语义（D5）**：一场默认扣 1 课时；实际上课不足/超时按实扣，支持两位小数
   （0.5 / 0.67 / 1.5…）。金额 = 实扣课时 × 该生该科账户单价（**单价服务端取，工具不传金额**）。
   「1 课时 = 多长墙钟时间」由各账户自定（可能 1h 也可能 1.5h），系统不做时长换算。

🔴 **幂等（uk(session_id, flow_type)）**：同一场次重复结算不会重复扣费——BE 把该场放进
   返回的 `skipped:[{session_id, reason}]`，其余场次照常落账（逐场独立事务）。
   故 settle_sessions 可安全重放；**看到 skipped 不要改 id 重试**，先看 reason。

🔴 **可逆**：已结场次改「请假/取消」（走 update_session 的 leave/cancel）→ BE 同事务自动冲正
   流水、余额恢复、settle_status 置已冲正、未填内容的反馈壳删除。本组不提供独立冲正工具。

对接 BE（ScheduleSessionController + SettlementService，契约正本 = PRD-015 §10.2）：
  GET  /teacher/schedule/settle/pending → [{sessionId,date,start,end,targetName,subject,
                                            subjectLabel,planLessonTitle,price}]
  POST /teacher/schedule/settle {items:[{sessionId,hours?,timeNote?}], genFeedback}
                               → {settled, feedbackSheetIds:[str], skipped:[{sessionId,reason}]}
"""
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/schedule"


def _sid(v) -> Optional[str]:
    """id 一律出字符串（None 保持 None）—— 雪花号绝不以数字回传给模型。"""
    return None if v is None else str(v)


def _pending_row(r: dict) -> dict:
    """BE 待结算行 → 工具口径（键名沿 BE/FE 契约 PendingSettlementVO，只把 id 拧成字符串）。"""
    out = dict(r)
    out["sessionId"] = _sid(r.get("sessionId"))
    return out


def _skip_row(r: dict) -> dict:
    return {"session_id": _sid(r.get("sessionId")), "reason": r.get("reason")}


def _norm_items(items) -> tuple[list, Optional[str]]:
    """入参 items（snake_case）→ BE items（camelCase）。返回 (items, err)。"""
    if not isinstance(items, list) or not items:
        return [], "items 不能为空：[{session_id, hours?, time_note?}]"
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return [], f"items[{i}] 需为对象 {{session_id, hours?, time_note?}}，收到 {type(it).__name__}"
        sid = it.get("session_id", it.get("sessionId"))
        if sid is None or not str(sid).strip():
            return [], f"items[{i}] 缺 session_id（字符串传，list_pending_settlements 查）"
        # 🔴 str 传：雪花号走 JSON double 会截尾成死 id（create_paper 同型坑）
        row: dict = {"sessionId": str(sid).strip()}
        hours = it.get("hours")
        if hours is not None:
            try:
                h = float(hours)
            except (TypeError, ValueError):
                return [], f"items[{i}] hours 需为数字（课时，支持两位小数），收到 {hours!r}"
            if h <= 0:
                return [], f"items[{i}] hours 需 > 0（不扣课时就别放进 items），收到 {h}"
            row["hours"] = round(h, 2)
        note = it.get("time_note", it.get("timeNote"))
        if note:
            row["timeNote"] = str(note)
        out.append(row)
    return out, None


# ═════════════════════ MCP 工具注册 ═════════════════════
def register(mcp, client: RuoyiClient) -> None:

    @mcp.tool(tags={"prep"})
    async def list_pending_settlements() -> dict:
        """列出「过点未结算」的场次（纯读，不动账）→ {ok, rows, total}。

        口径 = 结束时间已过 && 场次状态=已排 && 未结算 的**本人**场次（按日期+起始时间升序）；
        只含学生一对一场次（班课不接账户/结算），排除外部占位。

        rows=[{sessionId(str), date, start, end, targetName, subject, subjectLabel,
               planLessonTitle, price}]：
          price = 该生该科账户单价（元/课时）；🔴 **price=null 表示这科还没开户**——
          该场照常列出但结算会被 skipped，先让老师去开户（本工具面不含开户，走平台账户区块）。

        🔴 本工具只提醒不扣费；扣费必须老师明确确认后调 settle_sessions（D4）。
        典型用法：每晚提醒 / 老师问「有哪些课还没消」→ 调它 → 把清单念给老师 → 等确认再结算。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            r = await client.teacher_get(f"{BASE}/settle/pending", {})
        except RuoyiError as e:
            return {"ok": False, "reason": f"取待结算清单失败: {e}"}
        rows = r if isinstance(r, list) else ((r or {}).get("rows") or [])
        rows = [_pending_row(x) for x in rows if isinstance(x, dict)]
        return {"ok": True, "rows": rows, "total": len(rows)}

    @mcp.tool(tags={"prep"})
    async def settle_sessions(items: list[dict], gen_feedback: bool = True) -> dict:
        """一键结算已上课的场次（**扣课时课费**，老师确认后才调）→ {ok, settled, feedback_sheet_ids, skipped}。

        每场结算 = 三连（同事务，任一步失败整场回滚）：
          ① 流水：课时 -实扣数、金额 -实扣数×账户单价（挂 session_id 作幂等键）
          ② 场次：状态=已上 + 结算态=已结
          ③ 反馈壳：gen_feedback=True 时自动建一张空反馈单，绑该场次与其计划，
             计划内序号 lesson_seq 自动 = 该计划现有反馈数+1（之后用 upsert_feedback_sheet
             带 sheet_id 往壳里补五列内容，别新建重复单）

        参数:
          items: [{session_id, hours?, time_note?}]
            session_id : 🔴 **字符串**雪花号（list_pending_settlements 拿，严禁编造/别传数字）
            hours      : 实扣课时，**不传 = 1**（D5）。上了 2/3 节课就传 0.67，上了一个半就传 1.5，
                         两位小数；金额由服务端按账户单价算，**工具不传金额**。
            time_note  : 实际上课时间备注（如「09:05-10:40」），写进流水 note，台账可见
          gen_feedback: 是否同时建反馈壳（默认 True = 平台弹窗默认勾选口径 D12）

        返回:
          settled            本次真正落账的场次数
          feedback_sheet_ids 联动生成的反馈单 id（字符串数组；gen_feedback=False 时为空）
          skipped            [{session_id, reason}] 被跳过的场次 —— 未开户 / **已结算（幂等）** /
                             已冲正 / 班课 / 外部占位 / 场次不存在或无权。
                             🔴 跳过≠失败：其余场次已照常落账，把 reason 原样告诉老师，
                             **别换 id 重试、别重复调用想"补上"**（重复调只会再被幂等挡回）。

        🔴 扣费不可儿戏：老师没明确说结算/消课，绝不调本工具；一次只结算老师点名的场次。
        🔴 结错了怎么办：把该场次改「请假/取消」（update_session action=leave|cancel）→
           BE 自动冲正返还课时课费、空反馈壳删除，不要用调整流水去手工抹平。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        norm, err = _norm_items(items)
        if err:
            return {"ok": False, "reason": err}
        body = {"items": norm, "genFeedback": bool(gen_feedback)}
        try:
            r = await client.teacher_post(f"{BASE}/settle", body) or {}
        except RuoyiError as e:
            return {"ok": False, "reason": f"结算失败: {e}"}
        if not isinstance(r, dict):
            return {"ok": False, "reason": f"settle 返回异常: {str(r)[:200]}"}
        skipped = [_skip_row(x) for x in (r.get("skipped") or []) if isinstance(x, dict)]
        return {
            "ok": True,
            "settled": r.get("settled"),
            "feedback_sheet_ids": [str(x) for x in (r.get("feedbackSheetIds") or [])],
            "skipped": skipped,
            "requested": len(norm),
        }
