""""🔥 当前最活跃 Top-N"作者排行(数据驱动、透明、每次渲染重算)。

范围：Top-3(JF/JFE/RFS) ChatGPT 后语料。
指数(透明、展示在页面)：
    score = h-index + log₁₀(1 + 总被引)
其中 h-index 限定在本语料内(被引≥h 次的论文数 h)；总被引来自 OpenAlex 的
`cited_by_count`(我们 enrich/citations 提前补好)。同时展示 论文数 / 总被引 /
h-index 三个原始数，让用户一眼看穿计算逻辑。

设计取舍(诚实)：
  - 起算阈值 ≥2 篇(单篇高被引的偶然性高，排除噪音；与"持续活跃"语义一致)。
  - 同名风险无法靠算法解决(如 Kai Li 是 2 人)，由页面文案提示用户留意。
  - 别名(featured_aliases)沿用展示层规范化，让一人不同写法合并计分。
"""

import math
from collections import Counter, defaultdict

from src.highlight import _normalize, load_highlight_config

_TOP3 = {"JF", "JFE", "RFS"}
_MIN_PAPERS = 2


def active_top_n(papers: list[dict], n: int = 10) -> list[dict]:
    cfg = load_highlight_config()
    aliases = cfg.get("featured_aliases") or {}
    alias_norm = {_normalize(k): v for k, v in aliases.items() if k and v}
    featured_norm = {_normalize(a) for a in cfg.get("featured_authors", []) if a}
    # 已在 featured_authors 里的别名也算"已curated"(用于在表上标 ⭐)
    featured_norm |= {_normalize(v) for v in aliases.values() if v}

    def canon(name: str) -> str:
        return alias_norm.get(_normalize(name or ""), name)

    by_key: dict[str, list[int]] = defaultdict(list)
    spellings: dict[str, Counter] = defaultdict(Counter)
    for p in papers:
        if p.get("source") not in _TOP3:
            continue
        cbc = p.get("cited_by_count")
        cbc = int(cbc) if isinstance(cbc, int) else 0
        for a in (p.get("authors") or []):
            disp = canon(a)
            key = _normalize(disp)
            if not key:
                continue
            by_key[key].append(cbc)
            spellings[key][disp] += 1

    rows: list[dict] = []
    for key, cites in by_key.items():
        if len(cites) < _MIN_PAPERS:
            continue
        cites_sorted = sorted(cites, reverse=True)
        total = sum(cites_sorted)
        # h-index：cites_sorted 倒序，找最大 h 使得第 h 篇被引≥h
        h = 0
        for i, c in enumerate(cites_sorted, 1):
            if c >= i:
                h = i
            else:
                break
        score = h + math.log10(1 + total)
        sp = spellings[key].most_common()
        disp = next((s for s, _ in sp if not s.isupper()), sp[0][0])
        rows.append({
            "name": disp,
            "papers": len(cites_sorted),
            "citations": total,
            "h_index": h,
            "score": round(score, 2),
            "is_curated": key in featured_norm,
        })
    rows.sort(key=lambda r: (-r["score"], -r["papers"], r["name"].lower()))
    return rows[:n]
