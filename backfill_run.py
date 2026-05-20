"""历史回填执行器（分阶段、断点续传）。

用法（已激活 venv，项目根目录）：
    python backfill_run.py finance5     # JF/JFE/RFS/JFQA/JAR 全收
    python backfill_run.py MS           # MS，过强金融/AI 预筛
    python backfill_run.py NBER         # NBER(10.3386 前缀)，过预筛

机制（复用主流水线的容错哲学）：
  - 走 Crossref 回填（from-created-date≥2022-11-30，offset 分页）。
  - 与归档按指纹去重：已总结过的（指纹∈seen）直接跳过 -> 天然断点续传。
  - 历史文 first_seen = 其 published_date（不是今天）——不污染首页"最新更新"。
  - 每 CHECKPOINT 篇就并入归档并写 papers.json/seen.json：中断/停掉后
    重跑同一阶段会自动从断点续上，不重复花钱。
  - 单篇总结失败只记日志、不计入归档 -> 下次重跑自动重试。
  - 阶段跑完重渲染多页站。
"""

import logging
import sys
from datetime import datetime

from main import (_CN_TZ, PAPERS_PATH, load_papers, save_papers)
from src.dedup import hash_paper, load_seen, save_seen
from src.enrich import enrich_missing_abstracts
from src.fetchers.crossref import fetch_crossref_backfill
from src.highlight import annotate_all
from src.relevance import is_finance_or_ai_relevant
from src.renderer import render
from src.summarizer import SummarizeError, summarize_paper

logger = logging.getLogger("backfill")

_FINANCE5 = [
    ("JF", "1540-6261"),
    ("JFE", "0304-405X"),
    ("RFS", "1465-7368"),
    ("JFQA", "1756-6916"),
    ("JAR", "1475-679X"),
]
_CHECKPOINT = 25  # 每总结这么多篇就写盘一次（断点续传粒度）


def _fetch_stage(stage: str) -> list[dict]:
    if stage == "finance5":
        out: list[dict] = []
        for name, issn in _FINANCE5:
            out.extend(fetch_crossref_backfill(name, issn=issn))  # 不过滤
        return out
    if stage == "MS":
        return fetch_crossref_backfill(
            "MS", issn="1526-5501", relevance=is_finance_or_ai_relevant)
    if stage == "NBER":
        return fetch_crossref_backfill(
            "NBER", prefix="10.3386", relevance=is_finance_or_ai_relevant)
    raise SystemExit(f"未知阶段：{stage}（用 finance5 / MS / NBER）")


def main(stage: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("========== 回填阶段 [%s] 开始 ==========", stage)
    today = datetime.now(_CN_TZ).strftime("%Y-%m-%d")

    archive = load_papers()
    seen = load_seen()
    # first_seen 创世回填（与 main 一致，幂等）
    for p in archive:
        if not p.get("first_seen"):
            p["first_seen"] = p.get("published_date") or today
    logger.info("归档已有 %d 篇；已见指纹 %d 个", len(archive), len(seen))

    fetched = _fetch_stage(stage)
    logger.info("[%s] Crossref 回填抓到 %d 篇", stage, len(fetched))

    # 去重：跳过已总结过的（断点续传的核心）
    new: list[dict] = []
    seen_run: set[str] = set()
    for p in fetched:
        h = hash_paper(p)
        if h in seen or h in seen_run:
            continue
        seen_run.add(h)
        new.append(p)
    logger.info("[%s] 去重后待处理 %d 篇（其余已总结过，跳过）", stage, len(new))
    if not new:
        logger.info("[%s] 没有新论文，直接重渲染收尾", stage)

    # 补摘要（Elsevier→S2）：历史文很多缺摘要（JFE/NBER）
    if new:
        enrich_missing_abstracts(new)

    archive_by_hash = {hash_paper(p): p for p in archive}
    pending: list[dict] = []
    done = failed = 0

    def flush():
        """把 pending 并入归档并落盘（断点）。已有的优先保留。"""
        nonlocal pending
        if not pending:
            return
        for p in pending:
            archive_by_hash.setdefault(hash_paper(p), p)
        merged = list(archive_by_hash.values())
        save_papers(merged)
        save_seen({hash_paper(p) for p in merged})
        logger.info("checkpoint：已并入归档并写盘，归档总计 %d 篇", len(merged))
        pending = []

    for i, paper in enumerate(new, 1):
        # 历史文：first_seen = 发表日（不是今天），不污染"最新更新"栏
        paper["first_seen"] = paper.get("published_date") or today
        title = paper.get("title", "<无标题>")
        logger.info("[%s %d/%d] %s | %s",
                    stage, i, len(new), paper.get("source"), title[:56])
        try:
            pending.append(summarize_paper(paper))
            done += 1
        except SummarizeError:
            failed += 1
            logger.error("跳过（总结失败，下次重跑自动重试）：%s", title[:56])
        except Exception:
            failed += 1
            logger.exception("跳过（未预期错误，下次重试）：%s", title[:56])
        if len(pending) >= _CHECKPOINT:
            flush()

    flush()
    logger.info("[%s] 总结完成：成功 %d，失败 %d", stage, done, failed)

    # 重渲染多页站
    final = load_papers()
    out = render(annotate_all(final))
    logger.info("========== 回填阶段 [%s] 结束 | 归档 %d 篇 | 网页 %s ==========",
                stage, len(final), out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python backfill_run.py finance5|MS|NBER")
    main(sys.argv[1])
