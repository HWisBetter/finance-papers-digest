"""标题层面的"非论文"识别 —— 跨数据源统一、单一来源。

为什么独立成模块：JF/JAR 走 Wiley、JFE/MS/RFS 走 Crossref，
两条抓取路径都要按同一套规则判定"卷首页/通告"和"被撤稿论文"，
fetcher 之间不该互相 import，highlight 也要用 `is_retracted_title`，
所以抽到这个中立模块，谁都从这里拿，判定逻辑只有一份。

两条规则，刻意分开（这是和用户敲定的撤稿政策）：
  - is_noise_title  : 卷首页/行政/**撤稿通告(RETRACTION)** -> 抓取时直接剔除。
  - is_retracted_title : **被撤稿的论文本体(RETRACTED: ...)** -> 它是真发表过的
    文章，按"绝不丢真文章"原则【保留】，只在页面上打"⚠ 已撤稿"标记。

denylist（前缀白名单）而非 allowlist：宁可漏过个别噪音，也绝不误杀真论文。
"""

import re

# JF 等会把"奖项得主/人物致敬"页标题写成纯人名（如 "Andrew W. Lo"、
# "Steven N. Kaplan"），它们不是论文、没有摘要。只认【名 + 单字母缩写点 + 姓】
# 这种带中间缩写点的形式——真研究论文的整条标题绝不可能长这样，零误杀风险。
# （没有中间缩写点的纯人名不强行匹配，避免误伤 "Bank Runs" 这类两词真标题。）
_PERSON_NAME_RE = re.compile(r"^[A-Z][a-z]+ [A-Z]\.(?: [A-Z]\.)? [A-Z][a-z'’-]+$")


# 故意【不含 "retracted"】：被撤稿论文本体要保留+打标，不在此处剔除。
NOISE_TITLE_PREFIXES = (
    "editorial board",
    "editorial statement",
    "expression of concern",
    "corrigendum",
    "corrigenda",
    "erratum",
    "errata",
    "retraction",          # 撤稿"通告"："Retraction"/"Retraction notice"/"Retraction:"
    "issue information",
    "front matter",
    "frontmatter",
    "back matter",
    "acknowledgment of referees",
    "acknowledgement of referees",
    "acknowledgment to referees",
    "acknowledgement to referees",
    "list of referees",
    "call for papers",
    "in memoriam",
    "publisher's note",
    "publisher’s note",
    "masthead",
    "table of contents",
    # —— 2026-05-19 扩充：回填实测漏过的 JF/JFQA/JAR/RFS 通告类（按开头匹配）——
    "announcement",            # ANNOUNCEMENTS
    "american finance association",
    "report of the",           # Report of the Editor/Executive/2024 Annual...
    "correction to",           # Correction to: <orig title>（更正通告）
    "honoring ",               # Honoring Michael C. Jensen（致敬，非论文）
    "preliminary program",
    "participant schedule",
)

# 标题中【包含】即视为非论文（用于无法靠开头匹配的：卷封面页、后缀 ERRATUM、
# 奖项/审稿人名单等）。这些短语在真金融论文标题里几乎不可能出现 -> 极保守。
NOISE_TITLE_CONTAINS = (
    "cover and front matter",
    "cover and back matter",
    "front matter",
    "back matter",
    "excellence in refereeing",
    "annual membership meeting",
    ": winner of the",         # "<人名>: Winner of the 2025 Fischer Black Prize"
    "prizes for",              # "BRATTLE... PRIZES FOR 2024"
    "— erratum",
    "– erratum",
    "—erratum",
    "–erratum",
    " erratum",                # "... Relationship ERRATUM" 等后缀写法
)


def _norm(title: str) -> str:
    return " ".join((title or "").split()).strip().lower()


def is_noise_title(title: str) -> bool:
    """卷首页/行政/撤稿通告/纯人名简介页 -> True（抓取时剔除）。极保守。"""
    raw = " ".join((title or "").split()).strip()  # 保留大小写，供人名规则用
    if not raw:
        return False
    if _PERSON_NAME_RE.match(raw):  # "Andrew W. Lo" 这类奖项/致敬人名页
        return True
    t = raw.lower()
    if any(t.startswith(p) for p in NOISE_TITLE_PREFIXES):
        return True
    return any(c in t for c in NOISE_TITLE_CONTAINS)


def is_retracted_title(title: str) -> bool:
    """被撤稿的论文本体（标题以 "RETRACTED" 开头）-> True。

    覆盖 "RETRACTED:" / "RETRACTED ARTICLE:" / "Retracted ..." 等写法。
    这类文章【保留】，仅由上层打"已撤稿"标记，不剔除、不影响摘要/总结。
    """
    return _norm(title).startswith("retracted")
