"""G6（PRD-015 AC9）：教务线工具面 —— 向后兼容 + 新工具契约（纯本地，不碰库不碰网）。

🔴 本卡铁律 = **旧调用不传新参 = 旧行为**：故断言口径不是"新参能用"，而是
   ①老参一个不少、默认值一字不改 ②required 集合不扩大 ③新参全是可选。
   任何把新参写成必填、或给老参改默认值的改动，都会在这里红。
"""
import pytest
from fastmcp import Client

from teacher_mcp.server import build_server
from teacher_mcp.tools.feedback import _export_plan_png, _list_sheets, _upsert_sheet
from teacher_mcp.tools.settle import _norm_items


class FakeClient:
    """录线（不发网）：记下每次调用的 path/body/params，用来断言"下发了什么"。"""

    def __init__(self, post_resp=None):
        self.calls = []
        self._post_resp = post_resp or {}

    async def teacher_post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return self._post_resp

    async def teacher_put(self, path, body=None):
        self.calls.append(("PUT", path, body))
        return {}

    async def teacher_get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return {"rows": []}

    async def teacher_get_bytes(self, path, params=None):
        self.calls.append(("GET_BYTES", path, params))
        return b"\x89PNG_fake_bytes"


async def _schema(name: str) -> dict:
    async with Client(build_server("all")) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert name in tools, f"工具 {name} 未注册"
    return tools[name].inputSchema or {}


def _defaults(schema: dict) -> dict:
    return {k: v.get("default") for k, v in (schema.get("properties") or {}).items()}


# ═════════════ 兼容：老工具只加可选参 ═════════════

@pytest.mark.asyncio
async def test_upsert_feedback_sheet_is_additive():
    """upsert_feedback_sheet：老参 7 个默认值不变、required 仍只有 target_id/title，新参可选。"""
    s = await _schema("upsert_feedback_sheet")
    d = _defaults(s)
    # 改前基线（本卡动手前实录）：老参名 + 默认值逐个钉死
    legacy = {"target_id": None, "title": None, "lesson_date": "", "rows": None,
              "sheet_id": "", "batch_key": "", "lesson_seq": 0}
    for k, v in legacy.items():
        assert k in d, f"老参 {k} 丢了（破坏兼容）"
        assert d[k] == v, f"老参 {k} 默认值漂移: {d[k]!r} != {v!r}"
    assert sorted(s.get("required") or []) == ["target_id", "title"], "required 扩大 = 旧调用会炸"
    for k in ("session_id", "plan_id"):
        assert d.get(k) == "", f"新参 {k} 必须是可选且缺省空串（缺省即旧行为）"


@pytest.mark.asyncio
async def test_list_feedback_sheets_is_additive():
    """list_feedback_sheets：老参默认值不变、required 仍为空，新增 plan_id 可选。"""
    s = await _schema("list_feedback_sheets")
    d = _defaults(s)
    for k in ("target_id", "keyword", "batch_key"):
        assert d.get(k) == "", f"老参 {k} 默认值漂移/丢失: {d.get(k)!r}"
    assert not (s.get("required") or []), "list 系工具不该有必填参"
    assert d.get("plan_id") == "", "新参 plan_id 必须可选"


# ═════════════ 新工具契约 ═════════════

@pytest.mark.asyncio
async def test_export_feedback_plan_png_schema():
    """export_feedback_plan_png：plan_id 必填字符串（雪花 str 铁律），mode 缺省 single（D13）。"""
    s = await _schema("export_feedback_plan_png")
    assert sorted(s.get("required") or []) == ["plan_id"]
    props = s.get("properties") or {}
    assert props["plan_id"].get("type") == "string", "雪花 id 必须 string，integer 会截尾"
    assert _defaults(s).get("mode") == "single"


@pytest.mark.asyncio
async def test_settle_tools_schema():
    """list_pending_settlements 无参；settle_sessions items 必填 + gen_feedback 缺省 True（D12）。"""
    s = await _schema("list_pending_settlements")
    assert not (s.get("required") or [])
    assert not (s.get("properties") or {}), "待结算清单是无参纯读工具"

    s = await _schema("settle_sessions")
    assert sorted(s.get("required") or []) == ["items"]
    assert _defaults(s).get("gen_feedback") is True


# ═════════════ 结算入参归一化（纯函数，扣费前最后一道本地闸） ═════════════

@pytest.mark.parametrize("items,err_kw", [
    ([], "不能为空"),
    (None, "不能为空"),
    (["352"], "需为对象"),
    ([{"hours": 1}], "缺 session_id"),
    ([{"session_id": "352", "hours": 0}], "> 0"),
    ([{"session_id": "352", "hours": -1}], "> 0"),
    ([{"session_id": "352", "hours": "半节"}], "需为数字"),
])
def test_norm_items_rejects(items, err_kw):
    out, err = _norm_items(items)
    assert err and err_kw in err, f"{items!r} 应被拒: {err!r}"
    assert out == []


def test_norm_items_maps_to_be_contract():
    """snake_case → camelCase；🔴 session_id 一律出**字符串**（雪花号走 double 会截尾成死 id）。"""
    out, err = _norm_items([
        {"session_id": 2082723049878183938},                 # 数字传进来也要拧成 str
        {"session_id": "352", "hours": 0.666, "time_note": "09:05-10:40"},
    ])
    assert err is None
    assert out[0] == {"sessionId": "2082723049878183938"}
    assert isinstance(out[0]["sessionId"], str)
    assert out[1] == {"sessionId": "352", "hours": 0.67, "timeNote": "09:05-10:40"}


def test_norm_items_default_hours_left_to_be():
    """不传 hours = 不下发字段，由 BE 兜 1 课时（D5）——MCP 不在本地假设默认值。"""
    out, err = _norm_items([{"session_id": "352"}])
    assert err is None and "hours" not in out[0]


# ═════════════ 线上下发什么（FakeClient 录线，不依赖 BE 是否已上批4） ═════════════

@pytest.mark.asyncio
async def test_upsert_sends_new_ids_as_string():
    """新参真下发且是**字符串**；🔴 不传新参时 body 里连键都不出现（= 旧请求逐字节不变）。"""
    fc = FakeClient({"id": 9})
    await _upsert_sheet(fc, 75, "标题", "2026-07-29", [], session_id=361, plan_id=61)
    _, path, body = fc.calls[-1]
    assert path == "/teacher/feedback/sheet"
    assert body["sessionId"] == "361" and isinstance(body["sessionId"], str)
    assert body["planId"] == "61" and isinstance(body["planId"], str)

    fc = FakeClient({"id": 9})
    await _upsert_sheet(fc, 75, "标题", "2026-07-29", [])          # 旧式调用
    _, _, body = fc.calls[-1]
    assert sorted(body) == ["lessonDate", "rows", "targetId", "title"], body
    assert "sessionId" not in body and "planId" not in body and "lessonSeq" not in body


@pytest.mark.asyncio
async def test_list_sends_plan_id_only_when_given():
    fc = FakeClient()
    await _list_sheets(fc, plan_id=61)
    assert fc.calls[-1][2] == {"planId": "61"}
    fc = FakeClient()
    await _list_sheets(fc, target_id="75")                        # 旧式调用
    assert fc.calls[-1][2] == {"targetId": "75"}                  # 不夹带 planId


@pytest.mark.asyncio
async def test_export_plan_png_body_and_marker(tmp_path, monkeypatch):
    """POST 体 = {planId(str), mode}；产物落本地返 [[FILE:]] marker（PRD-009 鉴权坑口径）。"""
    from teacher_mcp import config
    monkeypatch.setattr(config.settings, "feedback_out_dir", str(tmp_path))
    fc = FakeClient({"file": "x.png", "mode": "long", "sheetCount": 3})
    r = await _export_plan_png(fc, 61, "long")
    assert fc.calls[0][1] == "/teacher/feedback/export-plan-png"
    assert fc.calls[0][2] == {"planId": "61", "mode": "long"}
    assert r["ok"] and r["mode"] == "long" and r["sheet_count"] == 3
    assert r["file_marker"] == f"[[FILE:{r['local_path']}]]"
    assert r["bytes"] > 0


@pytest.mark.asyncio
async def test_export_plan_png_rejects_bad_mode():
    fc = FakeClient()
    r = await _export_plan_png(fc, 61, "长图")
    assert r["ok"] is False and not fc.calls, "非法 mode 必须本地拦下，不该打 BE"
