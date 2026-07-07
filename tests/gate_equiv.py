"""G5（AC2）：转换器平移等价回归 —— 旧仓 app.* vs 新仓 teacher_mcp.domains.* 。

两层断言：
  ① 源码级：domains 四文件与旧仓对应文件逐行 diff，差异行只允许是 import 行（\xa0 字节漂移零容忍）；
  ② 运行时：paperparse 关键函数（含 \xa0 敏感的 strip_source_prefix / 全量 parse_paper）
     对固定样例，旧新输出严格相等。
旧仓只读。老仓路径不存在时本 gate 明确 FAIL（不是 skip——等价基线丢了必须知道）。
"""
import importlib
import sys
from pathlib import Path

OLD_ROOT = Path(r"D:\workplace\ai-bkb\teacher-mcp")
NEW_ROOT = Path(__file__).resolve().parent.parent

PAIRS = [
    ("app/paperparse.py", "src/teacher_mcp/domains/paperparse.py"),
    ("app/docconv.py", "src/teacher_mcp/domains/docconv.py"),
    ("app/lectureconv.py", "src/teacher_mcp/domains/lectureconv.py"),
    ("app/dicts.py", "src/teacher_mcp/domains/dicts.py"),
]


def _is_import_line(s: str) -> bool:
    t = s.strip()
    return t.startswith(("from app.", "from teacher_mcp.", "import app.", "import teacher_mcp."))


def test_source_equiv_only_import_lines():
    assert OLD_ROOT.exists(), f"等价基线丢失：旧仓不存在 {OLD_ROOT}"
    import difflib

    for old_rel, new_rel in PAIRS:
        old = (OLD_ROOT / old_rel).read_bytes().decode("utf-8").splitlines()
        new = (NEW_ROOT / new_rel).read_bytes().decode("utf-8").splitlines()
        bad = []
        for line in difflib.unified_diff(old, new, lineterm="", n=0):
            if line[:1] in "+-" and line[:3] not in ("+++", "---"):
                if not _is_import_line(line[1:]):
                    bad.append(line)
        assert not bad, f"{old_rel} 非 import 行漂移: {bad[:5]}"


# ── 运行时等价：\xa0 敏感样例（NBSP 前缀 / 全角空格 / 常规） ──
_STEMS = [
    "（2024·杭州中考）如图，在△ABC 中，AB=AC。",
    "(2023\xa0学年期末)\xa0计算：$2x+3=7$。",
    "【2025 模拟】下列说法正确的是（  ）",
    "无前缀的普通题干，含\xa0NBSP\xa0在中间。",
]

_PAPER = (
    "一、选择题\n"
    "1.（2024·绍兴期中）计算 $(-2)^{2}$ 的结果是（  ）\n"
    "A. 4 B. -4 C. 2 D. -2\n"
    "2. 下列各数中最小的是（  ）\n"
    "A. 0 B. -1 C. 1 D. -2\n"
    "二、填空题\n"
    "3.\xa0（2023 学年月考）若 $x=1$，则 $x+1=$ ______。\n"
    "参考答案\n"
    "1. A\n2. D\n3. 2\n"
)


def _mods():
    sys.path.insert(0, str(OLD_ROOT))
    try:
        old = importlib.import_module("app.paperparse")
    finally:
        sys.path.remove(str(OLD_ROOT))
    new = importlib.import_module("teacher_mcp.domains.paperparse")
    return old, new


def test_runtime_equiv_paperparse():
    old, new = _mods()
    for s in _STEMS:
        assert old.strip_source_prefix(s) == new.strip_source_prefix(s), repr(s)
        assert old.plain_text(s) == new.plain_text(s), repr(s)
    assert old.parse_paper(_PAPER) == new.parse_paper(_PAPER)
    oq, nq = old.parse_paper(_PAPER), new.parse_paper(_PAPER)
    # 绝对值断言防两边同错为空。注：第 3 题题号后跟 \xa0，新旧解析器一致不认（行为等价即达标），
    # 故样例实际拆出 2 题——这本身就是一条 \xa0 行为等价的活证据。
    assert len(nq) >= 2, f"样例应拆出≥2题, 实得 {len(nq)}"
    for a, b in zip(oq, nq):
        assert old.infer_type(a.get("stem", ""), a.get("options"), None, "") == \
               new.infer_type(b.get("stem", ""), b.get("options"), None, "")
