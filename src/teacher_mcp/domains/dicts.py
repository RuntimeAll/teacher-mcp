"""字典常量 = sys_dict_data 的代码侧镜像（单一事实源，别在别处散落魔法值）。

🔴 题型 / 难度 / 来源三个字典的唯一镜像。底座 sys_dict_data 改了须同步这里
   （或后续加 list_dict MCP 工具运行时动态拉取，替代本文件）。
   value↔label 必须与 sys_dict_data 完全一致 —— 前端按字典 value→label 渲染，错位 = 题型/来源显示错。
"""

# ── biz_question_type 题型（sys_dict_data dict_code 177-184，全 8 类）──
QUESTION_TYPE = {
    1: "选择题", 2: "判断题", 3: "应用题", 4: "填空题",
    5: "解答题", 6: "作图题", 7: "计算题", 8: "证明题",
}

# ── biz_question_difficulty 难度（185-188）= biz_question.difficult / dim4_difficulty ──
QUESTION_DIFFICULTY = {1: "基础", 2: "中等", 3: "较难", 4: "压轴"}

# ── biz_question.source_type 列实际渲染用的来源码 = 前端 book-ui SOURCE_TYPE_LABELS 硬编码约定
#    （editor.vue:119；与 ingest_paper.derive_source_type 一致。月考/单元在此有码）──
SOURCE_TYPE = {
    1: "中考真题", 2: "模拟", 3: "期末", 4: "月考", 5: "单元", 6: "自编", 9: "其他",
}

# ⚠ 陷阱：sys_dict_data 另有 biz_question_source 字典（dict_code 189-196），约定完全不同
#    （1教材/2质检/3竞赛/4中考/5模拟/6自编/7期中/8期末），但它【未绑定】到 source_type 列——
#    source_type 列只认上面的 SOURCE_TYPE。别把这俩弄混。
QUESTION_SOURCE_DICT = {
    1: "教材/同步", 2: "质检/调研", 3: "竞赛", 4: "中考真题",
    5: "模拟卷", 6: "自编/原创", 7: "期中", 8: "期末",
}

# ── DNA 打标字符串码字典（biz_anno_*，dict_value = 字符串本身）──
#    DNA 打标只能从这些受控词表里选，别塞自由文本。value=label（字符串即码）。
#    底座 sys_dict_data 的 biz_anno_* 改了须同步这里。顺序与字典 dict_sort 一致。
#    COG 认知层级（了解<理解<掌握<灵活运用）有递进语义；其余为并列枚举。
ANNO_COG = ("了解", "理解", "掌握", "灵活运用")
ANNO_ERROR = ("概念混淆", "计算失误", "审题偏差", "隐含遗漏", "分类不全", "表达不规范", "思路缺失")
ANNO_LITERACY = ("抽象", "运算", "几何直观", "空间观念", "推理", "模型观念", "数据观念", "应用意识", "创新意识")
ANNO_METHOD = ("数形结合", "分类讨论", "化归转化", "方程函数", "数学建模", "特殊与一般", "待定系数", "数学归纳")
ANNO_SCENE = ("纯数学", "现实生活", "科学跨学科", "数学文化")

# dict_type → 受控词表（tuple，有序），打标时按维度取值域校验。
ANNO_DICTS = {
    "biz_anno_COG": ANNO_COG,
    "biz_anno_ERROR": ANNO_ERROR,
    "biz_anno_LITERACY": ANNO_LITERACY,
    "biz_anno_METHOD": ANNO_METHOD,
    "biz_anno_SCENE": ANNO_SCENE,
}


def is_valid_anno(dict_type, value):
    """value 是否在 dict_type 的受控词表内（打标落库前的字符串码校验）。"""
    vocab = ANNO_DICTS.get(dict_type)
    return vocab is not None and value in vocab


# 拆题用：题型中文关键词 → 题型码。证明/应用 排在解答前，二者同现时先命中专用类型。
SECTION_TYPE_KEYWORDS = [
    ("选择", 1), ("判断", 2), ("填空", 4), ("作图", 6),
    ("计算", 7), ("证明", 8), ("应用", 3),
    ("解答", 5), ("简答", 5), ("综合", 5), ("探究", 5),  # 综合/探究无专用字典 → 归解答(5)
]


def section_type_of(line):
    """从章节头/题面文字判题型码（命中 SECTION_TYPE_KEYWORDS 首个关键词），无 → None。"""
    for kw, code in SECTION_TYPE_KEYWORDS:
        if kw in line:
            return code
    return None
