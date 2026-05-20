"""月度被引数刷新：重新拉 JF/JFE/RFS 论文的 cited_by_count。

每月1日由 GitHub Actions 自动触发，也可手动跑：
    .venv/Scripts/python.exe refresh_citations.py
"""
import json
import logging
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

from src.citations import fetch_cited_by_count
from src.highlight import annotate_all
from src.renderer import render

REFRESH_SOURCES = {"JF", "JFE", "RFS"}
CHECKPOINT_EVERY = 50
_SLEEP = 0.15   # OpenAlex polite rate


def main() -> None:
    path = Path("data/papers.json")
    papers = json.loads(path.read_text(encoding="utf-8"))

    candidates = [p for p in papers
                  if p.get("source") in REFRESH_SOURCES and p.get("doi")]
    logger.info("刷新被引数：%d 篇 JF/JFE/RFS 论文（共 %d 篇）", len(candidates), len(papers))

    updated = 0
    for i, p in enumerate(candidates, 1):
        new_val = fetch_cited_by_count(p["doi"])
        if new_val is not None:
            old = p.get("cited_by_count")
            if new_val != old:
                p["cited_by_count"] = new_val
                updated += 1
        if i % CHECKPOINT_EVERY == 0:
            path.write_text(json.dumps(papers, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            logger.info("[%d/%d] checkpoint，已更新 %d 篇", i, len(candidates), updated)
        if i < len(candidates):
            time.sleep(_SLEEP)

    path.write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("被引数刷新完成：%d / %d 篇有变化", updated, len(candidates))

    # 重新渲染（活跃榜排名随之更新）
    highlighted = annotate_all(papers)
    render(highlighted, Path("docs"))
    logger.info("网站重新渲染完成")


if __name__ == "__main__":
    main()
