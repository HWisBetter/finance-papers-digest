"""给每篇论文补 OpenAlex 的被引数(`cited_by_count`)。

用途：算"🔥 当前活跃 Top-10"的复合指数(h-index + log₁₀(1+总被引))需要每篇被引数。
我们已经在补摘要时用过 OpenAlex，免 key、免登录。这里只取一个标量字段，更轻。

设计原则同 enrich：尽力而为、绝不阻塞流水线；失败的下次再补(resumable，
按 `cited_by_count is None` 判定未处理，有值则跳过)。
"""

import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_URL = "https://api.openalex.org/works/https://doi.org/{doi}"
_UA = "finance-papers-digest/1.0 (mailto:digest@example.com)"
_SLEEP = 0.12   # 同 OpenAlex 礼貌池速率


def fetch_cited_by_count(doi: str) -> int | None:
    """OpenAlex /works 单查，返回 cited_by_count；任何失败返回 None。"""
    if not doi:
        return None
    url = _URL.format(doi=urllib.parse.quote(doi))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
    except Exception:
        return None
    v = data.get("cited_by_count")
    return int(v) if isinstance(v, int) else None


def backfill_citations(papers: list[dict], lookup_doi_fn, save_fn=None,
                       checkpoint_every: int = 25) -> int:
    """对所有 cited_by_count 未设置的论文逐一查 OpenAlex、就地填进 p['cited_by_count']。
    lookup_doi_fn: 给定 paper -> DOI（包括 NBER 的 10.3386 还原）。
    save_fn(optional): 每 checkpoint_every 篇调用一次用来写盘(传 main.save_papers 即可)。
    返回成功补的篇数。
    """
    todo = [p for p in papers if p.get("cited_by_count") is None]
    logger.info("待补被引数 %d 篇(总 %d，已存 %d)",
                len(todo), len(papers), len(papers) - len(todo))
    filled = 0
    for i, p in enumerate(todo, 1):
        doi = lookup_doi_fn(p) if lookup_doi_fn else ""
        n = fetch_cited_by_count(doi)
        if n is not None:
            p["cited_by_count"] = n
            filled += 1
        else:
            p["cited_by_count"] = 0   # 没查到当 0 处理，避免每次都重试
        if i % 50 == 0:
            logger.info("[%d/%d] 已查 %d，最近 %s -> %s",
                        i, len(todo), filled, doi[:40], p.get("cited_by_count"))
        if save_fn and i % checkpoint_every == 0:
            save_fn(papers)
            logger.info("checkpoint：写盘 %d/%d", i, len(todo))
        if i < len(todo):
            time.sleep(_SLEEP)
    if save_fn:
        save_fn(papers)
    logger.info("被引数补全完成：补 %d / 共 %d", filled, len(todo))
    return filled
