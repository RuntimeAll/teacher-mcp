"""MCP 工具·计算题出题器（口算/横式，人教版小学 1-6 年级计算谱系）。

三工具（agent 一键出计算题，确定性程序生成不走 LLM）：
  list_calc_types      类型全表（46 类：一年级 5 以内 → 六年级分数百分比比例）
  generate_calc_paper  按 groups 出卷 → 题目卷 + 答案卷双 PDF（系统版式）
  generate_calc_items  🆕 只出题目数据 {q,a} 不渲染 PDF —— agent 塞进自有版面（每日一练等）

设计对齐（与 special.py 同构）：
  - register(mcp, client) 注入单 RuoyiClient；写工具先 client.has_session()。
  - 生成在 BE（OralCalcService）：数域/进退位/整除/去重约束内置，seed 复现同卷。
  - tags={"prep"}：随备课角色暴露（口算卷是备课卷位①的标配材料）。

对接 BE（OralCalcController）：
  GET  /teacher/oralcalc/types     类型全表 [{code,name,grade,term}]
  POST /teacher/oralcalc/export    {title?, seed?, withGroupLabel?, fillRows?, groups:[…]}
                                   → {questionUrl, answerUrl, total, seed}
  POST /teacher/oralcalc/generate  同入参 → {total, seed, groups:[{label,cols,mode,items:[{q,a}]}]}
"""
from pydantic import BaseModel, Field

from teacher_mcp.backends.ruoyi import RuoyiClient, RuoyiError

BASE = "/teacher/oralcalc"


class CalcGroup(BaseModel):
    """一组题：type=类型码（list_calc_types 查），count=题数，label=分组标记，level=难度档。"""

    type: str = Field(description="类型码（如 add20c/sub20b/multable/fracdiff，严禁编造，list_calc_types 查全表）")
    count: int = Field(default=12, description="该组题数（1-200，全卷总数 ≤200）")
    label: str = Field(default="", description="分组标记（🔴 卷面规范 2026-07-19：内容名不上卷面，只印「一、二」序号标识；此字段仅作分组开关/内部记录）")
    level: str = Field(default="", description="难度档：basic 基础 / advanced 提高；空=原随机行为。🔴 当前仅三年级计算谱系生效（mul2d2d/mul1d/div1d/dec1/add3d），其余类型忽略该字段。难度由客观开关控制：进位次数/因数含0/首位是否够除/是否退位")


def _payload(groups: list[CalcGroup]) -> list[dict]:
    """CalcGroup → BE 入参（空字段不传，保持 BE 侧缺省语义）。"""
    return [
        {
            "type": g.type,
            "count": g.count,
            **({"label": g.label} if g.label else {}),
            **({"level": g.level} if g.level else {}),
        }
        for g in groups
    ]


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

    # ═════════════ 2. generate_calc_paper：一键出卷（系统版式 PDF） ═════════════
    @mcp.tool(tags={"prep"})
    async def generate_calc_paper(groups: list[CalcGroup], title: str = "口算训练",
                                  seed: str = "", with_group_label: bool = True,
                                  with_answer: bool = False, fill_rows: bool = True) -> dict:
        """一键生成计算题卷（确定性程序生成非 LLM）→ 题目卷 PDF（口算主场景不出答案卷）。

        按 groups 逐组生成（每组一个类型一个多栏区块），约束内置：进退位可控、除法整除/
        有余数分型、分数自动约分/假分数化带分数、全卷跨组去重；题目卷带「姓名/用时/做对」栏。
        参数:
          groups: [{type, count, label?, level?}]，type 从 list_calc_types 查（严禁编造）。
          title:  卷名（卷面可见，如"口算训练③（10分钟）"）。
          seed:   随机种子（任意字符串，同串复现同一份卷；空=随机）。
          with_group_label: False 时不印组标（整卷混排风格）。🔴 组标卷面只印「一、二」
            序号标识、绝不印练习内容名（学生自明，规范 2026-07-19）。
          with_answer: True 才附带教师答案卷（🔴 口算卷默认不出，高年级分数/方程需核对时才开）。
          fill_rows: 缺省 True = 每组题数向上凑整到栏数倍数（网格每行凑满，故实际题数可能
            多于 count）；要「填几题出几题」传 False。
        返回: {ok, question_url, answer_url?, total, seed}；未登录/未知类型 → {ok:false, reason}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not groups:
            return {"ok": False, "reason": "groups 不能为空：[{type, count}]"}
        body = {
            "title": title,
            "withGroupLabel": with_group_label,
            "fillRows": fill_rows,
            "papers": ["question", "answer"] if with_answer else ["question"],
            "groups": _payload(groups),
        }
        if seed:
            body["seed"] = seed
        try:
            r = await client.teacher_post(f"{BASE}/export", body) or {}
            return {
                "ok": True,
                "question_url": r.get("questionUrl"),
                "answer_url": r.get("answerUrl"),
                "total": r.get("total"),
                "seed": r.get("seed"),
            }
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}

    # ═════════════ 3. generate_calc_items：只出数据，自排版用 ═════════════
    @mcp.tool(tags={"prep"})
    async def generate_calc_items(groups: list[CalcGroup], seed: str = "",
                                  fill_rows: bool = False) -> dict:
        """只出计算题数据、不渲染 PDF —— 拿 {q,a} 塞进自有版面（每日一练等）。

        与 generate_calc_paper 同一套生成器（确定性、跨组去重、难度档、seed 复现），
        区别 = 不出 PDF、直接返回题面与答案，排版归 agent。
        🔴 题目由程序生成，答案由生成器同步算出——不需要再人工/LLM 验算。
        参数:
          groups: [{type, count, label?, level?}]，type 从 list_calc_types 查（严禁编造）；
            level=basic/advanced 出基础版/提高版（同考点不同难度，两版一次做完）。
          seed:   随机种子（任意字符串，同串复现同一批题；空=随机）。
          fill_rows: 🔴 缺省 False = 要几题给几题（自排版场景不该被静默改数量）；
            True 才向上凑整到栏数倍数。
        返回: {ok, total, seed, groups:[{label, cols, mode, items:[{q,a}]}]}。
        """
        if not client.has_session():
            return {"ok": False, "reason": "需先 login"}
        if not groups:
            return {"ok": False, "reason": "groups 不能为空：[{type, count}]"}
        body = {"fillRows": fill_rows, "groups": _payload(groups)}
        if seed:
            body["seed"] = seed
        try:
            r = await client.teacher_post(f"{BASE}/generate", body) or {}
            return {
                "ok": True,
                "total": r.get("total"),
                "seed": r.get("seed"),
                "groups": r.get("groups") or [],
            }
        except RuoyiError as e:
            return {"ok": False, "reason": str(e)}
