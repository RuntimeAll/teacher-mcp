"""MCP 工具·题库管理组：delete_questions 安全删题（v4 散题拍板 2026-07-29）。

删除走 backends.db pymysql 直连（BE 无删除端点；挂账同 db.py 头注释，HTTP 化随 PRD-005）。
🔴 安全模型 = 只能删"散题"：被试卷/导入卷/书架/教辅书/解题模型母题/活血缘母题/讲义引用的题
一律拒删（blocked 返回，要删先解绑）——载体引用闸让本工具天然删不掉任何"被包装过"的题。
"""
import asyncio

from teacher_mcp.backends import db as appdb
from teacher_mcp.backends.ruoyi import RuoyiClient


def register(mcp, client: RuoyiClient) -> None:

    @mcp.tool(tags={"data"})
    async def delete_questions(question_ids: list[str], reason: str = "",
                               confirm: bool = False, backup_dir: str = "") -> dict:
        """🔴 安全删题（只能删散题）。confirm=False（默认）=dry_run 预览：返回 deletable/blocked 清单不动库；
        confirm=True 才真删——删前自动备份主行+正文 JSON（返回 backup_path），随删全部附属表行并写审计。

        阻删闸（命中即拒删该 id）：试卷/导入卷/书架书/教辅书/解题模型母题/活血缘母题/讲义引用。
        单次上限 500；id 一律字符串传（雪花号截尾）。删除不可逆——真删前先 dry_run 给用户过目。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        actor = getattr(client, "username", None) or "agent"
        try:
            return await asyncio.to_thread(
                appdb.delete_questions_safe, question_ids, actor=str(actor), reason=reason,
                dry_run=not confirm, backup_dir=backup_dir or None)
        except Exception as e:  # noqa: BLE001 —— 直连失败给出可读原因
            return {"ok": False, "reason": f"删除失败: {e}"}
