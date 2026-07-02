"""一次性脚本：把生成好的 biz_subject 节点表灌进底座 /teacher/ingest/kg/tree。

用法：
  .venv\\Scripts\\python.exe tools\\ingest_kg.py <nodes.json> [chunk]
读 nodes.json = [{id,parentId,name,level,sort}, ...]，分块 upsert。
复用 app.ruoyi 客户端（双头鉴权 + envelope）。幂等 upsert，可重跑。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.ruoyi import RuoyiClient  # noqa: E402


async def main() -> None:
    nodes_path = sys.argv[1]
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    nodes = json.load(open(nodes_path, encoding="utf-8"))
    print(f"载入 {len(nodes)} 节点，chunk={chunk}")

    c = RuoyiClient()
    me = await c.login(settings.ruoyi_username, settings.ruoyi_password)
    print(f"登录 ok: {me}")

    total_upserted = 0
    for i in range(0, len(nodes), chunk):
        batch = nodes[i : i + chunk]
        try:
            resp = await c.teacher_post("/teacher/ingest/kg/tree", {"nodes": batch})
        except Exception as e:
            print(f"❌ 第 {i//chunk+1} 块失败（节点 {i}..{i+len(batch)}）: {e}")
            await c.aclose()
            sys.exit(1)
        n = (resp or {}).get("upserted") if isinstance(resp, dict) else resp
        total_upserted += n or 0
        print(f"  块{i//chunk+1}: 提交{len(batch)} → upserted={n}")
    print(f"✅ 完成，累计 upserted={total_upserted}")
    await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
