"""按 DNA JSON 批量打标 + 关系绑定（复用管线，换卷换 json）。

🔴 DNA 按【题号 num】组织（不绑 question_id → 免 snowflake 精度坑）；runner 用 --paper-id 从库解析 num→question_id，
   再 ① label_question（HTTP：难度/锚 dim1/dim5/DNA blob）② dbutil.write_relations（pymysql 补 biz_question_knowledge + biz_question_model）。
   一次把「难度 + 知识点锚 + 知识点表 + 模型链 + DNA」全写齐，不用再手工 pymysql 补。
用法: .venv\\Scripts\\python.exe tools\\label_runner.py --dna paperNN_dna.json --paper-id 18
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.dbutil import (  # noqa: E402
    errlog, model_gaps, propose_models, resolve_qids, set_paper_review, summarize_paper, write_relations)

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# label_question 工具入参白名单（DNA 记录里其余字段如 num/models/secondary_kps 不直接喂工具）
LABEL_FIELDS = {
    "difficult", "dim1_kp_id", "anchor_confidence", "need_anchor_review", "dim5_structure",
    "solution_skeleton", "assessment_type", "hard_points", "breakthrough_points", "tags",
    "scenario", "dna_type", "parametric_slots", "modeling_frame", "conditions", "variation_profile",
}


def _unwrap(result):
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    for c in getattr(result, "content", []) or []:
        if getattr(c, "text", None):
            try:
                return json.loads(c.text)
            except Exception:
                return {"_text": c.text}
    return {}


async def run(args):
    records = json.loads(Path(args.dna).read_text(encoding="utf-8"))
    num2id = resolve_qids(args.paper_id)
    print(f"[map] paper {args.paper_id} 题号→id 解析 {len(num2id)} 条")
    server = StdioServerParameters(command=PY, args=["-m", "app.server"], cwd=str(ROOT), env=dict(os.environ))
    ok = fail = 0
    dist = {1: 0, 2: 0, 3: 0, 4: 0}
    labeled = []
    async with stdio_client(server, errlog=errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            login = _unwrap(await session.call_tool("login", {}))
            assert login.get("ok"), f"login 失败 {login}"
            print(f"[login] teacher_id={login.get('teacher_id')}")
            for r in records:
                num = r.get("num")
                qid = num2id.get(num)
                if not qid:
                    fail += 1
                    print(f"  ❌ num={num} 库里无对应题（paper {args.paper_id}）")
                    continue
                payload = {k: v for k, v in r.items() if k in LABEL_FIELDS}
                payload["question_id"] = str(qid)   # 字符串传，pydantic 无损转 int
                res = _unwrap(await session.call_tool("label_question", payload))
                if res.get("ok"):
                    ok += 1
                    dist[r.get("difficult", 0)] = dist.get(r.get("difficult", 0), 0) + 1
                    rec = dict(r)
                    rec["question_id"] = qid
                    labeled.append(rec)
                    print(f"  ✓ num={num} q={qid} ★{r['difficult']} 锚={res.get('dim1_kp_id')}")
                else:
                    fail += 1
                    print(f"  ❌ num={num} {res.get('reason')}")
    new_n, _ = propose_models(labeled)       # 🔴 抽新模型(★3+无现成模型时)→回填 models[]
    kg_n, mdl_n = write_relations(labeled)   # pymysql 补关系表
    print(f"[done] 打标 ok={ok} fail={fail} | 知识点表+{kg_n} 模型链+{mdl_n} 抽新模型+{new_n} | 难度 ★1={dist[1]} ★2={dist[2]} ★3={dist[3]} ★4={dist[4]}")
    gaps = model_gaps(args.paper_id)         # 闸：★3+ 仍零模型 = 漏抽
    if gaps:
        print(f"  ⚠ 模型⟺难度闸未过：★3+ 题 {gaps} 仍无模型，请在 DNA 补 models 或 new_models")
    if args.review:                                # LLM 总结语 → biz_paper.remark（老师视角定性总评）
        set_paper_review(args.paper_id, Path(args.review).read_text(encoding="utf-8"))
        print(f"[review] LLM 总结语已写入 biz_paper.remark（paper {args.paper_id}）")
    print("\n" + summarize_paper(args.paper_id))   # 整卷总评（机器统计，落 .paper_summary_<id>.md）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dna", required=True)
    ap.add_argument("--paper-id", required=True, type=int)
    ap.add_argument("--review", default="", help="LLM 总结语 md 文件 → 写 biz_paper.remark")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
