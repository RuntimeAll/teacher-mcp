"""举一反三真链路冒烟（PRD-O-005 批3 / G4 复用）——到 compose-figure 为止，🔴 不调 persist。

前置：toolkit :9093 在跑（start-dev -Part tk）；book-server :9090 在跑；.env 有 admin 凭据。
跑法：.venv\\Scripts\\python.exe scripts\\smoke_variant.py
链路：login → make_variants(image_url) → [confirm] → generate_variants → verify_variant(1) → compose_variant_figure(1)。
LLM 轮 60~70s 属正常（timeout 600s）。每步打印 ok/status/关键字段。
"""
import asyncio
import sys
import time

# Windows stdout UTF-8（中文 JSON 免 GBK 乱码）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastmcp import Client

from teacher_mcp.server import build_server

# 配方里用过的那类 OSS 图（八下几何母题，stem-role 直链，公网可达）
IMAGE_URL = "https://ai-book.oss-cn-hangzhou.aliyuncs.com/2026/06/30/7a78827e1fd046d2b87d95f54cebaaf8.png"


def _t(sec: float) -> str:
    return f"{sec:.1f}s"


async def main() -> int:
    async with Client(build_server("variant")) as c:
        # 0. login（.env admin 兜底）
        t0 = time.time()
        r = (await c.call_tool("login", {})).data
        print(f"[login] ok={r.get('ok')} teacher_id={r.get('teacher_id')} ({_t(time.time()-t0)})")
        if not r.get("ok"):
            print("  登录失败，终止：", r)
            return 1

        # 1. 母题轮
        t0 = time.time()
        r = (await c.call_tool("make_variants", {"image_url": IMAGE_URL, "count": 3})).data
        thread_id = r.get("thread_id")
        status = r.get("status")
        mc = r.get("mother_card") or {}
        print(f"[make_variants] ok={r.get('ok')} status={status} thread={thread_id} ({_t(time.time()-t0)})")
        print(f"  mother_card.stem[:60]={str(mc.get('stem'))[:60]!r}")
        print(f"  anchor={ (mc.get('anchor') or {}) }")
        if not r.get("ok"):
            print("  母题轮失败，终止。hint=", r.get("hint"))
            return 1

        # 2. 确认章（如需）
        if status == "need_confirm":
            cands = r.get("kg_candidates") or []
            print(f"  need_confirm → kg_candidates={cands} flags={r.get('confirm_flags')}")
            chapter_id = (cands[0].get("chapter_id") if cands else None)
            if not chapter_id:
                print("  无 chapter_id 候选，无法确认，终止。")
                return 1
            t0 = time.time()
            r = (await c.call_tool("confirm_variant_chapter",
                                   {"thread_id": thread_id, "chapter_id": str(chapter_id)})).data
            print(f"[confirm_variant_chapter] ok={r.get('ok')} status={r.get('status')} chapter_id={chapter_id} ({_t(time.time()-t0)})")
            if not r.get("ok"):
                print("  确认失败，终止。", r.get("hint"))
                return 1

        # 3. 生成变式
        t0 = time.time()
        r = (await c.call_tool("generate_variants", {"thread_id": thread_id})).data
        variants = r.get("variants") or []
        print(f"[generate_variants] ok={r.get('ok')} count={r.get('count')} ({_t(time.time()-t0)})")
        for v in variants:
            print(f"  item_id={v.get('item_id')} qtype={v.get('qtype')} tier={v.get('tier')} "
                  f"stem[:40]={str(v.get('stem'))[:40]!r} has_figure_spec={bool(v.get('figure_spec'))}")
        if not r.get("ok") or not variants:
            print("  生成失败/空题组，终止。hint=", r.get("hint"))
            return 1

        first_id = variants[0].get("item_id")

        # 4. 单题验算
        t0 = time.time()
        r = (await c.call_tool("verify_variant", {"thread_id": thread_id, "item_id": first_id})).data
        print(f"[verify_variant] ok={r.get('ok')} item_id={first_id} verdict={r.get('verdict')} ({_t(time.time()-t0)})")
        print(f"  reason[:80]={str(r.get('reason'))[:80]!r} computed={r.get('computed')}")

        # 5. 配图
        t0 = time.time()
        r = (await c.call_tool("compose_variant_figure", {"thread_id": thread_id, "item_id": first_id})).data
        fs = r.get("figure_spec") or {}
        objs = fs.get("objects") if isinstance(fs, dict) else None
        print(f"[compose_variant_figure] ok={r.get('ok')} item_id={first_id} needs_figure={r.get('needs_figure')} ({_t(time.time()-t0)})")
        print(f"  figure_spec.bbox={fs.get('bbox') if isinstance(fs, dict) else None} "
              f"objects_n={len(objs) if isinstance(objs, list) else None}")

        print("\n🔴 冒烟到 compose-figure 为止，未调 persist（落库验证归 G4 gate）。")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
