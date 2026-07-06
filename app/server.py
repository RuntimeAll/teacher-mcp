"""PRD-C-1000 teacher-mcp —— RuoYi 平台能力 MCP 适配层（探针）。

本地 Claude Code 当编排层，经本 MCP server 把 RuoYi:8090 现有 HTTP 接口当工具调。
两底座（RuoYi:8090 / toolkit:8093）源码零改，本 server 是独立新增薄代理层。

🔴 多底座路由（RuoyiCluster）：A=:8080 主底座（现有全部工具打它）/ C=:8090 讲义接口待接
（同库同账号、token 互不通用 → 各自登录，C 线懒登录）/ toolkit=:8093 占位（FastAPI 非 RuoYi，未实现 client）。

🔴 加能力 = 加 tools/<name>.py + 下面 register 一行（AC6 解耦）：
   身份层(auth) / 读层(kg) / 写层(compose) 已分。二期 make_variants / ingest_textbook / build_kg
   照样各加一个 tools 模块 + register，login/鉴权/底座调用层不动。

传输 = stdio（本地 Claude Code 默认子进程最简）。
"""
import atexit
import os

from mcp.server.fastmcp import FastMCP

from app.ruoyi import RuoyiCluster
from app.tools import auth as tool_auth
from app.tools import compose as tool_compose
from app.tools import convert as tool_convert
from app.tools import ingest as tool_ingest
from app.tools import kg as tool_kg
from app.tools import label as tool_label
from app.tools import lecture as tool_lecture
from app.tools import lecture_read as tool_lecture_read
from app.tools import manual as tool_manual
from app.tools import qbank as tool_qbank
from app.tools import schedule as tool_schedule

mcp = FastMCP("teacher-mcp")

# 多底座会话簇（stdio 单进程）：_cluster.a=A 线主底座（现有工具全打它），_cluster.c=C 线懒登录。
_cluster = RuoyiCluster()


@atexit.register
def _cleanup() -> None:
    # 进程退出时尽量关连接（事件循环已停时忽略异常）
    try:
        import anyio

        anyio.run(_cluster.aclose)
    except Exception:
        pass


# ── PRD-C-213 FEAT-002：按 TEACHER_MCP_ROLE 条件注册工具组（缺省 all=全量，向后兼容）──
# 三助手视图（备课/题目录入/讲义录入）各连接用 env 指定 ROLE + 自己账号，只见本职工具。
# 本阶段是「视图优化」非安全边界（/teacher/** 仍零 @SaCheckPermission，接口级收窄另立下一轮 PRD）。
_SHARED = {"login", "list_kg_tree", "resolve_kg", "search_questions", "get_question", "get_role_manual"}
_GROUPS = {
    "prep": {"create_teach_target", "list_teach_targets", "upsert_course_plan", "schedule_sessions",
             "list_schedule", "update_session", "build_prep_pack", "render_prep_pack", "submit_review",
             "get_student_profile", "get_plan_detail", "list_lecture_docs", "get_lecture_content",
             "compose_paper", "create_paper", "update_paper", "ingest_items", "upload_image"},
    "ingest": {"format_question", "upload_image", "ingest_question", "ingest_items", "verify_ingest",
               "convert_doc", "convert_pdf", "parse_paper_text", "label_question"},
    "lecture": {"convert_lecture_docx", "save_lecture_frag", "remove_lecture_frag",
                "list_lecture_docs", "get_lecture_content", "convert_doc", "convert_pdf", "parse_paper_text"},
}
_ROLE = (os.getenv("TEACHER_MCP_ROLE", "all") or "all").strip().lower() or "all"
_ALLOWED = None if _ROLE in ("all",) else (_SHARED | _GROUPS.get(_ROLE, set()))
_MANUAL_ROLE = "prep" if _ROLE == "prep" else "ingest"  # get_role_manual 缺省角色跟随 ROLE


class _RoleView:
    """按角色过滤 @tool 注册的 mcp 代理：allowed=None 全量；否则只注册名在 allowed 里的工具。
    resource()（说明书）及 run() 等其余属性透传真 mcp——resource 全角色可见。"""

    def __init__(self, mcp_, allowed):
        self._mcp = mcp_
        self._allowed = allowed

    def tool(self, *args, **kwargs):
        real = self._mcp.tool(*args, **kwargs)
        allowed, name = self._allowed, kwargs.get("name")

        def deco(fn):
            nm = name or getattr(fn, "__name__", None)
            return real(fn) if (allowed is None or nm in allowed) else fn

        return deco

    def __getattr__(self, k):
        return getattr(self._mcp, k)


reg = _RoleView(mcp, _ALLOWED)

# ── 工具注册（加能力 = 在此 register 一行）──
tool_auth.register(reg, _cluster)         # login（身份层：登 A 线 + 记凭据供 C 线懒登录，传 cluster）
tool_kg.register(reg, _cluster.a)         # list_kg_tree / resolve_kg（读层：整树 + 锚定查表）
tool_compose.register(reg, _cluster.a)    # compose_paper / create_paper / update_paper（写层·能力①组卷）
tool_ingest.register(reg, _cluster.a)     # format/upload/ingest_question + 🔴ingest_items 统一入库口（写层·能力②录入，PRD-C-208）
tool_label.register(reg, _cluster.a)      # label_question（打标层·能力③ DNA 打标：难度/锚/解法骨架/变式底料）
tool_convert.register(reg, _cluster.a)    # convert_doc / convert_pdf / parse_paper_text（转换层·确定性预处理，PRD-C-208）
tool_manual.register(reg, _cluster.a, _MANUAL_ROLE)  # 角色说明书进协议：resource + get_role_manual（缺省角色跟随 ROLE）
tool_lecture.register(reg, _cluster)      # convert_lecture_docx（确定性转换）+ save_lecture_frag（写层·能力④讲义录入，PRD-C-210）🔴打 C 线 :8090=传 _cluster，工具内 ensure_c() 懒登录
tool_schedule.register(reg, _cluster)     # 教学安排与备课闭环 11 工具（PRD-C-213 + R7a get_plan_detail）🔴全打 C 线 :8090=传 _cluster，工具内 ensure_c() 懒登录
tool_qbank.register(reg, _cluster)        # search_questions / get_question（题库读侧·备课圈料，PRD-C-213 R7a）🔴打 C 线 :8090
tool_lecture_read.register(reg, _cluster) # list_lecture_docs / get_lecture_content（讲义读侧·据讲义出题，PRD-C-213 R7a）🔴打 C 线 :8090
# 二期占位（同样「加一个 tools 目录 + register 一行」）：
#   from app.tools import variants as tool_variants; tool_variants.register(mcp, _cluster.a)   # make_variants（举一反三）
#   from app.tools import kgbuild as tool_kgbuild; tool_kgbuild.register(mcp, _cluster.a)       # build_kg


def main() -> None:
    mcp.run()  # 默认 stdio transport


if __name__ == "__main__":
    main()
