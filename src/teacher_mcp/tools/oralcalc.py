"""MCP 工具·计算题出题器（口算/横式，人教版小学 1-6 年级计算谱系）。

两工具（agent 一键出计算卷，确定性程序生成不走 LLM）：
  list_calc_types      类型全表（46 类：一年级 5 以内 → 六年级分数百分比比例）
  generate_calc_paper  按 groups:[{type,count}] 出卷 → 题目卷 + 答案卷双 PDF

设计对齐（与 special.py 同构）：
  - register(mcp, client) 注入单 RuoyiClient；写工具先 client.has_session()。
  - 生成在 BE（OralCalcService）：数域/进退位/整除/去重约束内置，seed 复现同卷。
  - tags={"prep"}：随备课角色暴露（口算卷是备课卷位①的标配材料）。

对接 BE（OralCalcController）：
  GET  /teacher/oralcalc/types    类型全表 [{code,name,grade,term}]
  POST /teacher/oralcalc/export   {title?, seed?, withGroupLabel?, groups:[{type,count,label?}]}
                                  → {questionUrl, answerUrl, total, seed}
"""
from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/oralcalc"


class CalcGroup(BaseModel):
    """一组题：type=类型码（list_calc_types 查），count=题数，label=组标题（缺省用类型名）。"""

    type: str = Field(description="类型码（如 add20c/sub20b/multable/fracdiff，严禁编造，list_calc_types 查全表）")
    count: int = Field(default=12, description="该组题数（1-200，全卷总数 ≤200）")
    label: str = Field(default="", description="卷面组标题覆盖（缺省=类型名；🔴 卷面可见，只写干净说法）")


def register(mcp, client: RuoyiClient) -> None:

    # ═════════════ 1. list_calc_types：类型全表 ═════════════
    @mcp.tool(tags={"prep"})
    async def list_calc_types() -> dict:
        """计算题出题器·类型全表（覆盖人教版小学 1-6 年级计算谱系，46 类）。

        每类一条 {code, name, grade(1-6), term(1上/2下)}：一年级 5以内加减/10以内/20以内进退位/
        两位数±一位数…，二年级 100以内/表内乘除/有余数除法…，三年级 三位数/乘除一位数/同分母分数/
        一位小数…，四年级 三位数乘两位数/四则混合/简算/小数加减…，五年级 小数乘除/解方程/异分母
        分数/公因数公倍数/约分…，六年级 分数乘除混合/百分数互化/化简比/解比例。
        返回: {ok, types:[{code,name,grade,term}]}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        try:
            r = await client.teacher_get(f"{BASE}/types")
            return {"ok": True, "types": r}
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}

    # ═════════════ 2. generate_calc_paper：一键出卷 ═════════════
    @mcp.tool(tags={"prep"})
    async def generate_calc_paper(groups: list[CalcGroup], title: str = "口算训练",
                                  seed: str = "", with_group_label: bool = True) -> dict:
        """一键生成计算题卷（确定性程序生成非 LLM）→ 题目卷 + 教师答案卷双 PDF。

        按 groups 逐组生成（每组一个类型一个多栏区块），约束内置：进退位可控、除法整除/
        有余数分型、分数自动约分/假分数化带分数、组内去重；题目卷带「姓名/用时/做对」栏。
        参数:
          groups: [{type, count, label?}]，type 从 list_calc_types 查（严禁编造）。
          title:  卷名（卷面可见，如"口算训练③（10分钟）"）。
          seed:   随机种子（同参数同 seed 复现同一份卷；空=随机）。
          with_group_label: False 时不印组标题（整卷混排风格）。
        返回: {ok, question_url, answer_url, total, seed}；未登录/未知类型 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not groups:
            return {"ok": False, "reason": "groups 不能为空：[{type, count}]"}
        body = {
            "title": title,
            "withGroupLabel": with_group_label,
            "groups": [
                {"type": g.type, "count": g.count, **({"label": g.label} if g.label else {})}
                for g in groups
            ],
        }
        if seed:
            body["seed"] = seed
        try:
            r = await client.teacher_post(f"{BASE}/export", body)
            r = r or {}
            return {
                "ok": True,
                "question_url": r.get("questionUrl"),
                "answer_url": r.get("answerUrl"),
                "total": r.get("total"),
                "seed": r.get("seed"),
            }
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
