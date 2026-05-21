"""给论文打高亮标记：AI 相关、关注作者（"大佬"）。

设计要点：
  - 纯函数、无副作用、确定性。每次构建时对【全量归档】重算，
    不写进 papers.json，也不和昂贵的 AI 总结耦合。
  - 规则全部来自 config/highlight.yaml，改配置下次跑就生效，
    现有论文无需重新总结即可被重新高亮。
  - 仅产出视觉标记用的标志位，不改变排序/筛选。

接口：
  load_highlight_config() -> dict
  annotate(paper, cfg)    -> paper 的副本，多加：
        is_ai:            bool         命中 AI 规则
        is_featured:      bool         命中关注作者
        featured_matched: list[str]    命中的作者名（用于展示/排查）
  annotate_all(papers, cfg) -> list[dict]
"""

import logging
import re
from pathlib import Path

import yaml

from src.textnoise import is_retracted_title

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _PROJECT_ROOT / "config" / "highlight.yaml"

# 配置缺失/损坏时的兜底默认（保证流水线不崩，只是高亮可能不全）
_DEFAULTS = {
    "ai_keywords": [
        "machine learning", "deep learning", "neural network",
        "reinforcement learning", "large language model", "language model",
        "natural language processing", "generative ai",
        "artificial intelligence", "transformer",
        "机器学习", "深度学习", "神经网络", "强化学习", "大语言模型",
        "大模型", "自然语言处理", "人工智能", "生成式",
    ],
    "ai_acronyms": ["ai", "llm", "nlp", "gpt"],
    "featured_authors": [],
    "featured_aliases": {},  # 变体名 -> 规范显示名（仅影响展示/分组，不影响匹配）
}


def _normalize(text: str) -> str:
    """小写 + 合并空白，用于姓名/文本的稳健比较（与 dedup 同思路）。"""
    if not text:
        return ""
    return " ".join(text.split()).lower()


def load_highlight_config() -> dict:
    """读 config/highlight.yaml；缺失/损坏/字段缺省时回退默认，不抛异常。"""
    cfg = dict(_DEFAULTS)
    if not _CONFIG_PATH.exists():
        logger.warning("highlight.yaml 不存在，使用内置默认规则：%s", _CONFIG_PATH)
        return cfg
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for key in ("ai_keywords", "ai_acronyms", "featured_authors"):
            val = data.get(key)
            if isinstance(val, list) and val:
                cfg[key] = val
        aliases = data.get("featured_aliases")
        if isinstance(aliases, dict):
            cfg["featured_aliases"] = aliases
    except (yaml.YAMLError, OSError) as ex:
        logger.warning("读取 highlight.yaml 失败（%r），使用内置默认", ex)
    return cfg


def _build_acronym_re(acronyms: list[str]) -> re.Pattern | None:
    """把缩写编译成按整词匹配的正则，避免 'Shanghai' 命中 'ai'。"""
    cleaned = [re.escape(a.strip()) for a in acronyms if a and a.strip()]
    if not cleaned:
        return None
    return re.compile(r"\b(" + "|".join(cleaned) + r")\b", re.IGNORECASE)


def annotate(paper: dict, cfg: dict) -> dict:
    """返回 paper 的副本，加上 is_ai / is_featured / featured_matched。"""
    # 待检文本：标题 + 摘要 + 关键词 + AI 总结，全部小写
    parts = [
        paper.get("title") or "",
        paper.get("abstract") or "",
        paper.get("summary") or "",
        " ".join(paper.get("keywords") or []),
    ]
    text = _normalize(" ".join(parts))

    kw_hit = any(_normalize(k) in text for k in cfg.get("ai_keywords", []) if k)
    acro_re = _build_acronym_re(cfg.get("ai_acronyms", []))
    acro_hit = bool(acro_re and acro_re.search(text))
    is_ai = kw_hit or acro_hit

    featured_set = {_normalize(a) for a in cfg.get("featured_authors", []) if a}
    matched = [
        a for a in (paper.get("authors") or [])
        if _normalize(a) in featured_set
    ]

    return {
        **paper,
        "is_ai": is_ai,
        "is_featured": bool(matched),
        "featured_matched": matched,
        # 被撤稿论文：保留条目，仅打"⚠ 已撤稿"标记（与用户敲定的撤稿政策）。
        # 纯由标题推导，和 is_ai 同样不写进 papers.json、改规则即时生效。
        "is_retracted": is_retracted_title(paper.get("title", "")),
    }


def annotate_all(papers: list[dict], cfg: dict | None = None) -> list[dict]:
    """对整批论文打标。cfg 不传则现读一次配置。"""
    cfg = cfg or load_highlight_config()
    annotated = [annotate(p, cfg) for p in papers]
    n_ai = sum(1 for p in annotated if p["is_ai"])
    n_feat = sum(1 for p in annotated if p["is_featured"])
    n_retr = sum(1 for p in annotated if p["is_retracted"])
    logger.info("高亮：AI 相关 %d 篇，关注作者 %d 篇，已撤稿 %d 篇（共 %d 篇）",
                n_ai, n_feat, n_retr, len(annotated))
    return annotated


if __name__ == "__main__":
    # 自测：用真实归档跑一遍，看打标结果，不调 API、不写文件。
    import json

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )
    archive = json.loads(
        (_PROJECT_ROOT / "data" / "papers.json").read_text(encoding="utf-8")
    )
    out = annotate_all(archive)
    print("\n命中 AI 的论文：")
    for p in out:
        if p["is_ai"]:
            print("  -", p["title"][:70])
    print("\n命中关注作者的论文：")
    hit = [p for p in out if p["is_featured"]]
    if not hit:
        print("  （当前归档里没有名单中的作者）")
    for p in hit:
        print(f"  - {p['title'][:60]}  ← {p['featured_matched']}")
