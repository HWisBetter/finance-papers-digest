"""从 Top-3(JF/JFE/RFS) 的归档里统计高频作者，给"大佬名单"扩充提供候选。

零 API 成本、纯数据驱动、可复现。后续 SSRN 启用 + 名单维护时随时可以再跑。

启发：在最难的三本金融顶刊上反复出现(后 ChatGPT 时期 ~3.5 年)的作者，
是"大佬"的强信号；用户审过后把名字加进 config/highlight.yaml::featured_authors。
"""

import collections
import json
from pathlib import Path

from src.highlight import _normalize, load_highlight_config

_TOP3 = {"JF", "JFE", "RFS"}
_MIN_COUNT = 3       # 至少 3 篇 Top-3 才列为候选（少于此噪音多）
_SHOW_TOP = 40       # 最多展示这么多候选


def _archive_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "papers.json"


def candidates() -> list[dict]:
    papers = json.loads(_archive_path().read_text(encoding="utf-8"))
    cfg = load_highlight_config()
    excluded = {_normalize(n) for n in cfg.get("featured_authors", []) if n}
    aliases = cfg.get("featured_aliases") or {}
    excluded |= {_normalize(v) for v in aliases.values() if v}

    counts: collections.Counter = collections.Counter()
    samples: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    spellings: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in papers:
        if p.get("source") not in _TOP3:
            continue
        for a in p.get("authors") or []:
            key = _normalize(a)
            if not key or key in excluded:
                continue
            counts[key] += 1
            spellings[key][a] += 1
            if len(samples[key]) < 2:
                samples[key].append((p.get("source", ""), (p.get("title") or "")[:65]))

    out = []
    for key, n in counts.most_common():
        if n < _MIN_COUNT:
            break
        # 显示拼写：在该作者所有拼写里挑频次最高、且不是全大写的（更可读）
        cands = spellings[key].most_common()
        disp = next((s for s, _ in cands if not s.isupper()), cands[0][0])
        out.append({
            "display": disp,
            "norm": key,
            "count": n,
            "all_spellings": [s for s, _ in cands],
            "sample_papers": samples[key],
        })
    return out[:_SHOW_TOP]


def print_report() -> None:
    cands = candidates()
    print(f"Top-3(JF/JFE/RFS) ChatGPT 后高频作者候选 — Top {len(cands)}")
    print(f"(阈值 ≥ {_MIN_COUNT} 篇；已排除现 featured_authors 与别名)\n")
    for i, c in enumerate(cands, 1):
        spell = ", ".join(c["all_spellings"][:3])
        if len(c["all_spellings"]) > 3:
            spell += f"  … 共 {len(c['all_spellings'])} 种写法"
        print(f"{i:>2}. {c['display']}  —  {c['count']} 篇  [拼写: {spell}]")
        for src, title in c["sample_papers"]:
            print(f"     · [{src}] {title}")
        print()


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_report()
