"""MCP 工具·课后反馈单组（PRD-009，封 book-server /teacher/feedback，PRD-004 已 live）。

课后反馈机器人主链：老师发学生作业照片 → agent 多模态 Read 看图提炼五列 →
  upsert_feedback_sheet 建单 → export_feedback_png 导家长版 PNG（工具带 teacher token
  把图下到本机，返回 file_marker）→ bot 据 marker 把图内联发回飞书。

🔴 export 为何要本地落图：/teacher/schedule/artifact 是 @SaCheckLogin，bot 裸下载会 401；
   故由本工具（已持登录 teacher token）下载 bytes 写本机，bot 读本地文件免鉴权、也跨 Aliyun→101。

五列 = 序号 seq / 所属模块 module / 学习内容 content / 掌握情况 mastery / 不足点 weakness（全自由文本）。
🔴 家长可见卷面绝不出现内部词（层/★/素材/薄弱/挑题）——掌握情况写「熟练/基本掌握/待巩固」这类家长能懂的话。

🔴 PRD-015（2026-07-30）绑定语义升级（**旧调用不传新参 = 旧行为，一字不变**）：
  - D6 反馈**绑场次**：`session_id` 主绑定 + 冗余 `plan_id`（按计划查/导）；`batch_key` 降级为
    遗留字段（只读不删，老批次单照常可用）。🔴 本条翻案 PRD-010「批次独立不绑计划」。
  - D7 `lesson_seq` 语义 = **计划内反馈序号**，不传由 BE 自动填 `count(plan_id)+1`；
    导出黄条标题 =「{序号} · {上课日期}」，**全程不出现「第几次/第 N 节课」字样**。
  - D13 `export_feedback_plan_png(plan_id, mode)`：按计划出图，single（缺省）= 最新一单单张，
    long = 全量按序号升序拼长图。
  - D8 出口：**一律推老师本人飞书会话**（下载 PNG + bot 发老师自己），无「发送家长」功能，
    家长侧由老师人工转发。
"""
import os
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError
from teacher_mcp.config import settings

BASE = "/teacher/feedback"
ARTIFACT_PATH = "/teacher/schedule/artifact"


async def _list_sheets(client, target_id=None, keyword=None, batch_key=None, plan_id=None) -> dict:
    params: dict = {}
    if target_id:
        params["targetId"] = str(target_id)
    if keyword:
        params["keyword"] = keyword
    if batch_key:
        params["batchKey"] = batch_key
    if plan_id:
        params["planId"] = str(plan_id)  # PRD-015 D6：按课程计划过滤
    resp = await client.teacher_get(f"{BASE}/sheet/page", params)
    rows = resp.get("rows", []) if isinstance(resp, dict) else (resp or [])
    return {"ok": True, "rows": rows, "total": len(rows)}


async def _get_sheet(client, sheet_id) -> dict:
    resp = await client.teacher_get(f"{BASE}/sheet/{sheet_id}", {})
    return {"ok": True, "sheet": resp}


async def _upsert_sheet(client, target_id, title, lesson_date, rows, sheet_id=None,
                        batch_key=None, lesson_seq=None, session_id=None, plan_id=None) -> dict:
    body = {
        "targetId": str(target_id),
        "title": title or "",
        "lessonDate": lesson_date or "",
        "rows": rows or [],
    }
    if batch_key:
        body["batchKey"] = batch_key
    # 🔴 PRD-015 D6：绑场次/绑计划（雪花号一律 str 进 body，防 JSON double 截尾）
    if session_id:
        body["sessionId"] = str(session_id)
    if plan_id:
        body["planId"] = str(plan_id)
    if lesson_seq is not None and int(lesson_seq) > 0:
        body["lessonSeq"] = int(lesson_seq)
    if sheet_id:
        await client.teacher_put(f"{BASE}/sheet/{sheet_id}", body)
        return {"ok": True, "sheet_id": str(sheet_id), "updated": True}
    resp = await client.teacher_post(f"{BASE}/sheet", body)
    new_id = (resp or {}).get("id") if isinstance(resp, dict) else None
    return {"ok": True, "sheet_id": str(new_id) if new_id is not None else None, "updated": False}


async def _export_batch_png(client, target_id, batch_key=None) -> dict:
    """批次全量长图（PRD-010）：BE 拼图 → 带 token 下载落本机 → 返 file_marker。"""
    q = f"?targetId={target_id}"
    if batch_key:
        import urllib.parse
        q += "&batchKey=" + urllib.parse.quote(batch_key)
    resp = await client.teacher_post(f"{BASE}/batch/export-png{q}", {})
    file = (resp or {}).get("file") if isinstance(resp, dict) else None
    if not file:
        return {"ok": False, "error": f"batch export-png 未返回 file: {str(resp)[:200]}"}
    data = await client.teacher_get_bytes(ARTIFACT_PATH, {"path": file})
    out_dir = settings.feedback_out_dir or "/tmp"
    os.makedirs(out_dir, exist_ok=True)
    local_path = os.path.join(out_dir, f"fb_batch_{target_id}.png")
    with open(local_path, "wb") as f:
        f.write(data)
    return {
        "ok": True,
        "batch_key": (resp or {}).get("batchKey"),
        "sheet_count": (resp or {}).get("sheetCount"),
        "bytes": len(data),
        "local_path": local_path,
        "file_marker": f"[[FILE:{local_path}]]",
    }


async def _export_plan_png(client, plan_id, mode="single") -> dict:
    """按课程计划导出（PRD-015 D13）：single=最新一单单张 / long=全量按序拼长图。

    BE 出图 → 带 token 下载 artifact → 落本机返 file_marker（与单张/批次同一封装，
    PRD-009 artifact 端点要鉴权的坑已封在 teacher_get_bytes 里）。
    """
    m = (mode or "single").strip().lower()
    if m not in ("single", "long"):
        return {"ok": False, "error": f"mode 只能是 'single' 或 'long'，收到 {mode!r}"}
    resp = await client.teacher_post(
        f"{BASE}/export-plan-png", {"planId": str(plan_id), "mode": m}
    )
    file = (resp or {}).get("file") if isinstance(resp, dict) else None
    if not file:
        return {"ok": False, "error": f"export-plan-png 未返回 file: {str(resp)[:200]}"}
    data = await client.teacher_get_bytes(ARTIFACT_PATH, {"path": file})
    out_dir = settings.feedback_out_dir or "/tmp"
    os.makedirs(out_dir, exist_ok=True)
    local_path = os.path.join(out_dir, f"fb_plan_{plan_id}_{m}.png")
    with open(local_path, "wb") as f:
        f.write(data)
    return {
        "ok": True,
        "plan_id": str(plan_id),
        "mode": (resp or {}).get("mode") or m,
        "sheet_count": (resp or {}).get("sheetCount"),
        "bytes": len(data),
        "local_path": local_path,
        "file_marker": f"[[FILE:{local_path}]]",
    }


async def _export_png(client, sheet_id) -> dict:
    resp = await client.teacher_post(f"{BASE}/sheet/{sheet_id}/export-png", {})
    file = (resp or {}).get("file") if isinstance(resp, dict) else None
    if not file:
        return {"ok": False, "error": f"export-png 未返回 file: {str(resp)[:200]}"}
    # 🔴 带 teacher token 下载 artifact bytes（/teacher/schedule/artifact 需鉴权），写本机。
    #    不返回 http url——artifact 端点要鉴权，模型若把 url 写进回复 bot 裸下载只会得 401。
    data = await client.teacher_get_bytes(ARTIFACT_PATH, {"path": file})
    out_dir = settings.feedback_out_dir or "/tmp"
    os.makedirs(out_dir, exist_ok=True)
    local_path = os.path.join(out_dir, f"fb_export_{sheet_id}.png")
    with open(local_path, "wb") as f:
        f.write(data)
    return {
        "ok": True,
        "sheet_id": str(sheet_id),
        "bytes": len(data),
        "local_path": local_path,
        "file_marker": f"[[FILE:{local_path}]]",
    }


# ═════════════════════ MCP 工具注册 ═════════════════════
def register(mcp, client: RuoyiClient) -> None:
    @mcp.tool(tags={"prep"})
    async def list_feedback_sheets(target_id: str = "", keyword: str = "", batch_key: str = "",
                                   plan_id: str = "") -> dict:
        """列出当前老师名下的课后反馈单（owner 硬隔离）→ {ok, rows, total}。

        rows=[{id,targetId,targetName,sessionId,planId,batchKey,lessonSeq,title,lessonDate,...}]（新→旧）。
        🔴 改单前先用它找回目标单的 id，别新建重复单（结算自动建的**空壳**也在这里找，
           壳的 rows 为空、title 为空，往壳里补内容 = 带 sheet_id 调 upsert_feedback_sheet）。
        参数:
          target_id（可选，某学生）/ keyword（标题模糊）
          plan_id  （可选，🔴 PRD-015 现行：只看某课程计划下的反馈，看该计划已到第几号
                    → 序号由服务端自动递增，一般不用自己算）
          batch_key（可选，遗留 PRD-010 批次；新单不再用批次键）
        """
        try:
            return await _list_sheets(client, target_id or None, keyword or None,
                                      batch_key or None, plan_id or None)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def get_feedback_sheet(sheet_id: str) -> dict:
        """读一张反馈单详情（含五列 rows）→ {ok, sheet}。参数 sheet_id 字符串传。"""
        try:
            return await _get_sheet(client, sheet_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def upsert_feedback_sheet(
        target_id: str,
        title: str,
        lesson_date: str = "",
        rows: Optional[list] = None,
        sheet_id: str = "",
        batch_key: str = "",
        lesson_seq: int = 0,
        session_id: str = "",
        plan_id: str = "",
    ) -> dict:
        """建/改课后反馈单（归属当前登录老师）→ {ok, sheet_id}。

        🔴 **现行绑定口径（PRD-015 D6/D7）= 绑场次/绑计划，序号服务端自动**：
          - 结算（settle_sessions）已自动建好绑场次的**空壳**时 → 别新建！先
            `list_feedback_sheets(plan_id=…)` 找回壳的 id，带 `sheet_id` 往里补五列。
          - 手工建单 → 传 `session_id`（哪一场课）即可，不传 plan_id 时 BE 从场次回填计划；
            没排课的散单则直接传 `plan_id`（或都不传 = 遗留散单）。
          - `lesson_seq` **不传**：BE 自动 = 该计划下现有反馈数+1（计划内序号，导出黄条按它递增）。
            自己算序号只会撞号，除非老师明确要改某一单的序号，否则一律别传。
        🔴 batch_key = **遗留字段（PRD-010）**，只读不删：老单还带着它，新单不要再造批次键。

        参数:
          target_id  : 学生对象 id（字符串；先用 list_teach_targets 映射，严禁编造）
          title      : 标题（🔴 家长可见，禁内部词；可留空，导出时按「序号 · 上课日期」自动拼）
          lesson_date: 上课日期 yyyy-MM-dd（可选）
          rows       : 五列行数组 [{seq,module,content,mastery,weakness,kp_id?}]
          sheet_id   : 传了=改这张（PUT），不传=新建
          session_id : 🔴 绑定的场次 id（字符串雪花；list_schedule / list_pending_settlements 拿）
          plan_id    : 🔴 课程计划 id（字符串雪花；传 session_id 时可不传，BE 回填）
          batch_key  : 遗留批次键（只在改老单时沿用，新单别传）
          lesson_seq : 计划内序号（>0 才生效；🔴 缺省交 BE 自动递增，别自己算）
        🔴 掌握情况写「熟练/基本掌握/待巩固」等家长话术。
        """
        try:
            return await _upsert_sheet(
                client, target_id, title, lesson_date, rows or [], sheet_id or None,
                batch_key or None, lesson_seq if lesson_seq > 0 else None,
                session_id or None, plan_id or None,
            )
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def export_feedback_plan_png(plan_id: str, mode: str = "single") -> dict:
        """按**课程计划**导出反馈图（🔴 PRD-015 现行主路，发老师本人飞书用这个）
        → {ok, plan_id, mode, sheet_count, local_path, file_marker}。

        两种模式（D13）：
          single（缺省）= 该计划**最新一单**（序号最大的那节）单张出图 —— 刚补完这节反馈就发它
          long          = 该计划全部反馈单按序号升序拼**一张长图**（阶段汇总/家长要看全程时用）

        黄条标题按「序号 · 上课日期」递增，全程不出现"第几次/第N节课"字样；
        🔴 家长可见 → 图里零内部词（层/★/素材/薄弱/挑题）。

        🔴 导出后把 file_marker（[[FILE:…/fb_plan_*.png]]）**原样**写进回复（方括号内一字不改），
           机器人据此把图内联发回会话。出口 = 下载 + 发**老师本人**飞书，系统无"发送家长"功能，
           家长侧由老师自己转发（D8）。
        参数: plan_id 字符串雪花（get_plan_detail / list_feedback_sheets 拿）；mode 'single'|'long'。
        """
        try:
            return await _export_plan_png(client, plan_id, mode)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def export_feedback_batch_png(target_id: str, batch_key: str = "") -> dict:
        """批次全量导出（**PRD-010 遗留路径**）：该学生一个批次 1~N 节全部反馈单按课次拼
        **一张长图** → {ok, batch_key, sheet_count, local_path, file_marker}。

        🔴 新单一律绑计划不绑批次（PRD-015 D6）→ **现行导出走 `export_feedback_plan_png(plan_id)`**；
           本工具只在处理**老批次单**（带 batch_key 的历史单）时用。
        batch_key 缺省 = 该生最新批次。
        🔴 导出后把 file_marker（[[FILE:…/fb_batch_*.png]]）**原样**写进回复，
        机器人据此把长图内联发回会话（出口 = 老师本人飞书，无"发送家长"功能，D8）。
        """
        try:
            return await _export_batch_png(client, target_id, batch_key or None)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def export_feedback_png(sheet_id: str) -> dict:
        """把**单张**反馈单导成家长版 PNG 并下载到本机 → {ok, local_path, file_marker, ...}。

        🔴 常规出图走 export_feedback_plan_png（按计划，single=最新一单 / long=全量，PRD-015 现行）；
           本工具只在明确要"就这一张单、按 sheet_id 出图"时用。
        🔴 导出后必须把返回的 file_marker（形如 [[FILE:/tmp/fb_export_123.png]]）**原样**写进
           给用户的回复里（方括号内一字不改），飞书机器人据此把这张图内联发回会话。
        参数 sheet_id 字符串传。
        """
        try:
            return await _export_png(client, sheet_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
