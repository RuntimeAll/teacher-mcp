"""MCP 工具·课后反馈单组（PRD-009，封 book-server /teacher/feedback，PRD-004 已 live）。

课后反馈机器人主链：老师发学生作业照片 → agent 多模态 Read 看图提炼五列 →
  upsert_feedback_sheet 建单 → export_feedback_png 导家长版 PNG（工具带 teacher token
  把图下到本机，返回 file_marker）→ bot 据 marker 把图内联发回飞书。

🔴 export 为何要本地落图：/teacher/schedule/artifact 是 @SaCheckLogin，bot 裸下载会 401；
   故由本工具（已持登录 teacher token）下载 bytes 写本机，bot 读本地文件免鉴权、也跨 Aliyun→101。

五列 = 序号 seq / 所属模块 module / 学习内容 content / 掌握情况 mastery / 不足点 weakness（全自由文本）。
🔴 家长可见卷面绝不出现内部词（层/★/素材/薄弱/挑题）——掌握情况写「熟练/基本掌握/待巩固」这类家长能懂的话。
"""
import os
from typing import Optional

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError
from teacher_mcp.config import settings

BASE = "/teacher/feedback"
ARTIFACT_PATH = "/teacher/schedule/artifact"


async def _list_sheets(client, target_id=None, keyword=None, batch_key=None) -> dict:
    params: dict = {}
    if target_id:
        params["targetId"] = str(target_id)
    if keyword:
        params["keyword"] = keyword
    if batch_key:
        params["batchKey"] = batch_key
    resp = await client.teacher_get(f"{BASE}/sheet/page", params)
    rows = resp.get("rows", []) if isinstance(resp, dict) else (resp or [])
    return {"ok": True, "rows": rows, "total": len(rows)}


async def _get_sheet(client, sheet_id) -> dict:
    resp = await client.teacher_get(f"{BASE}/sheet/{sheet_id}", {})
    return {"ok": True, "sheet": resp}


async def _upsert_sheet(client, target_id, title, lesson_date, rows, sheet_id=None,
                        batch_key=None, lesson_seq=None) -> dict:
    body = {
        "targetId": str(target_id),
        "title": title or "",
        "lessonDate": lesson_date or "",
        "rows": rows or [],
    }
    if batch_key:
        body["batchKey"] = batch_key
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
    async def list_feedback_sheets(target_id: str = "", keyword: str = "", batch_key: str = "") -> dict:
        """列出当前老师名下的课后反馈单（owner 硬隔离）→ {ok, rows, total}。

        rows=[{id,targetId,targetName,batchKey,lessonSeq,title,lessonDate,...}]（新→旧）。
        🔴 改单前先用它找回目标单的 id，别新建重复单；🔴 接力新课次前先用它看该生
        最新批次已到第几节（batchKey+lessonSeq），新单 lesson_seq = 最大值 + 1。
        参数: target_id（可选）/ keyword（标题模糊）/ batch_key（只看某批次，PRD-010）。
        """
        try:
            return await _list_sheets(client, target_id or None, keyword or None, batch_key or None)
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
    ) -> dict:
        """建/改课后反馈单（归属当前登录老师）→ {ok, sheet_id}。

        🔴 PRD-010 批次模型（用户工作流=批次累积一次性全发）：一个学生一段课程 = 一个批次
        （batch_key 如「多多五上暑假数学」，独立概念**不绑课程计划**），批次内课次 lesson_seq
        依次递增。**接力建新课次单时必须带 batch_key + lesson_seq**（先 list_feedback_sheets
        看该生最新批次到第几节，新单 = 最大 lesson_seq + 1；title 缺省口径
        「{batch_key}第{N}节课上课内容」）。老师说"新开批次/新学期"才换新 batch_key 从 1 重计。

        参数:
          target_id  : 学生对象 id（字符串；先用 list_teach_targets 映射，严禁编造）
          title      : 标题（🔴 家长可见，禁内部词）
          lesson_date: 上课日期 yyyy-MM-dd（可选）
          rows       : 五列行数组 [{seq,module,content,mastery,weakness,kp_id?}]
          sheet_id   : 传了=改这张（PUT），不传=新建
          batch_key  : 批次键（接力单必带）
          lesson_seq : 批次内课次号（接力单必带，>0 生效）
        🔴 掌握情况写「熟练/基本掌握/待巩固」等家长话术。
        """
        try:
            return await _upsert_sheet(
                client, target_id, title, lesson_date, rows or [], sheet_id or None,
                batch_key or None, lesson_seq if lesson_seq > 0 else None,
            )
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def export_feedback_batch_png(target_id: str, batch_key: str = "") -> dict:
        """批次全量导出（PRD-010，🔴 发家长用这个不用单张）：该学生一个批次 1~N 节全部
        反馈单按课次拼**一张长图** → {ok, batch_key, sheet_count, local_path, file_marker}。

        batch_key 缺省 = 该生最新批次（新建课次后直接调它即可拿到含最新一节的全量图）。
        🔴 导出后把 file_marker（[[FILE:/tmp/fb_batch_*.png]]）**原样**写进回复，
        机器人据此把长图内联发回会话。
        """
        try:
            return await _export_batch_png(client, target_id, batch_key or None)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(tags={"prep"})
    async def export_feedback_png(sheet_id: str) -> dict:
        """把**单张**反馈单导成家长版 PNG 并下载到本机 → {ok, local_path, file_marker, ...}。

        🔴 发家长的常规场景请用 export_feedback_batch_png（批次全量长图，用户实发形态）；
           本工具只在明确要"单独看某一节"时用。
        🔴 导出后必须把返回的 file_marker（形如 [[FILE:/tmp/fb_export_123.png]]）**原样**写进
           给用户的回复里（方括号内一字不改），飞书机器人据此把这张图内联发回会话。
        参数 sheet_id 字符串传。
        """
        try:
            return await _export_png(client, sheet_id)
        except RuoyiError as e:
            return {"ok": False, "error": str(e)}
