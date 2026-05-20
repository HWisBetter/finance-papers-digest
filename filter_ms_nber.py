"""用 embedding 相似度把 MS/NBER 里的非金融论文从归档里剔除。

原理：
  以 JF + JFE + RFS 所有论文的 embedding 向量质心作为"金融语义中心"，
  计算每篇 MS/NBER 论文与该质心的余弦相似度，低于阈值的视为非金融、予以剔除。

用法：
  python filter_ms_nber.py                   # 用默认阈值 0.55，需交互确认
  python filter_ms_nber.py --threshold 0.60  # 自定义阈值
  python filter_ms_nber.py --dry-run         # 只统计，不改文件
  python filter_ms_nber.py --yes             # 跳过确认直接执行

运行后会：
  1. 打印各阈值下的过滤统计（方便你选阈值）
  2. 修改 data/papers.json（去掉被剔论文）
  3. 重算 data/seen.json
  4. 重建 docs/search-index.json（调用 build_search_index.py 逻辑）
  5. 重渲染整站 docs/
"""

import argparse
import json
import logging
import math
import sys
from pathlib import Path

# ---- UTF-8 控制台 ----
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("filter_ms_nber")

_ROOT = Path(__file__).resolve().parent
_PAPERS = _ROOT / "data" / "papers.json"
_SEEN = _ROOT / "data" / "seen.json"
_EMB = _ROOT / "data" / "embeddings.json"
_SEARCH_INDEX = _ROOT / "docs" / "search-index.json"

_TOP3 = {"JF", "JFE", "RFS"}
_TARGETS = {"MS", "NBER"}
_DEFAULT_THRESHOLD = 0.55


def _l2norm(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def compute_centroid(papers: list[dict], emb: dict) -> list[float] | None:
    """JF+JFE+RFS 全部论文的归一化向量平均 -> 再归一化 = 质心方向。"""
    from src.dedup import hash_paper
    vecs = []
    for p in papers:
        if p.get("source") not in _TOP3:
            continue
        h = hash_paper(p)
        v = emb.get(h)
        if v is None:
            continue
        vecs.append(_l2norm(v))
    if not vecs:
        return None
    dim = len(vecs[0])
    centroid = [0.0] * dim
    for v in vecs:
        for i, x in enumerate(v):
            centroid[i] += x
    n = len(vecs)
    centroid = [x / n for x in centroid]
    return _l2norm(centroid)


def score_papers(papers: list[dict], emb: dict, centroid: list[float]) -> list[tuple[dict, float]]:
    """返回 (paper, cosine_sim) 列表；无 embedding 的给 1.0（保留，不误删）。"""
    from src.dedup import hash_paper
    result = []
    for p in papers:
        h = hash_paper(p)
        v = emb.get(h)
        if v is None:
            result.append((p, 1.0))   # 无向量 -> 保守保留
        else:
            sim = _dot(_l2norm(v), centroid)
            result.append((p, sim))
    return result


def print_stats(scored: list[tuple[dict, float]], thresholds=(0.45, 0.50, 0.55, 0.60, 0.65)):
    """打印各阈值下会剔除多少篇，帮助选参数。"""
    total = len(scored)
    print(f"\n{'阈值':>8}  {'保留':>6}  {'剔除':>6}  {'剔除率':>7}")
    print("-" * 36)
    for thr in thresholds:
        keep = sum(1 for _, s in scored if s >= thr)
        drop = total - keep
        print(f"  {thr:.2f}   {keep:>6}  {drop:>6}  {drop/total*100:>6.1f}%")
    print()


def rebuild_search_index(papers: list[dict], emb: dict) -> None:
    """重建 docs/search-index.json（与 build_search_index.py 逻辑一致）。"""
    import base64
    from src.dedup import hash_paper

    items = []
    for p in papers:
        h = hash_paper(p)
        v = emb.get(h)
        if v is None:
            continue
        nv = _l2norm(v)
        # int8 量化
        ints = [max(-127, min(127, round(x * 127))) for x in nv]
        raw = bytes([b & 0xFF for b in ints])
        items.append({
            "t": p.get("title", ""),
            "u": p.get("url", ""),
            "s": p.get("source", ""),
            "tp": p.get("topic", ""),
            "v": base64.b64encode(raw).decode(),
        })

    index = {"dim": 384, "items": items}
    _SEARCH_INDEX.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    logger.info("重建 search-index.json：%d 篇", len(items))


def main():
    parser = argparse.ArgumentParser(description="MS/NBER 非金融论文 embedding 过滤")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD,
                        help=f"余弦相似度阈值（默认 {_DEFAULT_THRESHOLD}）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不修改文件")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args()

    # 加载
    logger.info("读取 papers.json …")
    papers = json.loads(_PAPERS.read_text(encoding="utf-8"))
    logger.info("读取 embeddings.json …")
    emb = json.loads(_EMB.read_text(encoding="utf-8"))

    # 计算质心
    logger.info("计算 JF/JFE/RFS 质心 …")
    centroid = compute_centroid(papers, emb)
    if centroid is None:
        logger.error("TOP3 论文无 embedding，无法计算质心，退出。")
        sys.exit(1)
    top3_count = sum(1 for p in papers if p.get("source") in _TOP3)
    logger.info("质心基于 %d 篇 TOP3 论文", top3_count)

    # 只对目标来源打分
    target_papers = [p for p in papers if p.get("source") in _TARGETS]
    other_papers  = [p for p in papers if p.get("source") not in _TARGETS]
    logger.info("待过滤：MS+NBER 共 %d 篇；其余 %d 篇不动", len(target_papers), len(other_papers))

    scored = score_papers(target_papers, emb, centroid)

    # 打印各阈值统计
    print_stats(scored)

    # 按选定阈值分组
    thr = args.threshold
    keep_target = [p for p, s in scored if s >= thr]
    drop_papers  = [p for p, s in scored if s < thr]

    print(f"选定阈值 {thr:.2f}：保留 {len(keep_target)} 篇，剔除 {len(drop_papers)} 篇")

    # 打印被剔论文样本（前 20）
    if drop_papers:
        print("\n[ 被剔除样本（前 20）]")
        drop_scored = [(p, s) for p, s in scored if s < thr]
        drop_scored.sort(key=lambda x: x[1])
        for p, s in drop_scored[:20]:
            print(f"  [{p['source']}] sim={s:.3f}  {p.get('title','')[:65]}")

    if args.dry_run:
        print("\n--dry-run 模式，不修改任何文件。")
        return

    # 确认
    if not args.yes:
        ans = input(f"\n确认剔除 {len(drop_papers)} 篇并更新文件？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    # 更新归档
    new_papers = other_papers + keep_target
    new_papers.sort(key=lambda p: (p.get("published_date") or "9999-99-99"), reverse=True)
    _PAPERS.write_text(json.dumps(new_papers, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("papers.json 已更新：%d 篇（剔除 %d）", len(new_papers), len(drop_papers))

    # 更新 seen.json
    from src.dedup import hash_paper, save_seen
    save_seen({hash_paper(p) for p in new_papers})
    logger.info("seen.json 已更新")

    # 重建 search-index.json
    rebuild_search_index(new_papers, emb)

    # 重渲染
    logger.info("重新渲染站点 …")
    from src.highlight import annotate_all
    from src.renderer import render
    highlighted = annotate_all(new_papers)
    render(highlighted)
    logger.info("完成！归档 %d 篇，剔除 %d 篇。", len(new_papers), len(drop_papers))


if __name__ == "__main__":
    main()
