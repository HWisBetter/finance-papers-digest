"""总指挥：把 fetchers → dedup → summarizer → renderer 串成一条流水线。

运行方式（已激活虚拟环境后，在项目根目录）：
    python main.py

流程：
    1. 读 seen.json（见过哪些论文的指纹）+ 读 papers.json（全量归档）
    2. 抓所有源（多期刊），任一源失败不影响其他
    3. 过滤掉已见过的（指纹在 seen 里的），只留新论文
    4. 缺摘要的（新论文 + 归档里 abstract_missing 的）先过 Semantic Scholar
       按 DOI 补摘要（JFE 等受益；补不到不报错，等下次重试）
    5. 给新论文盖日期章（NBER 取不到真日期才用今天 UTC+8）
    6. 逐篇调 AI 总结；单篇失败只记日志、跳过，不拖垮其他
    7. 归档里"曾经缺摘要、这次被补上"的，重做总结、原指纹原地替换（复活）
    8. 新论文并入归档，写 papers.json + seen.json
    9. 高亮打标 -> 渲染 docs/index.html（只显示最近 100 篇）

容错策略：
    - 一篇论文只有"总结成功并并入归档"后，它的指纹才算"已见"。
      失败的不写进归档/seen -> 下次运行自动重试（应对临时 API 故障）。
    - seen.json 每次都按"归档里有什么"重算，保证两者永远一致、能自愈。
    - 【可复活】abstract_missing 的归档文章不算"永久完成"：每次跑都重试
      补摘要，一旦补上就重做 AI 总结、按同一指纹原地替换归档条目。
      这刻意打破"指纹∈seen=永不再处理"的旧不变式，只对缺摘要的那批生效，
      与"失败不算已见、自动重试"的容错哲学一致。

冒烟测试：设环境变量 MAX_NEW_PAPERS=3 可只处理 3 篇新论文，
先小批量确认无误，再放开全量（首次全量约 60 篇 = 60 次 API 调用）。
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- Windows 控制台是 GBK，打印中文/外文会崩；启动时统一转 UTF-8 ----
# （兑现之前的承诺：以后直接 python main.py 跑，不用再手动设 PYTHONUTF8）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from src.dedup import hash_paper, load_seen, save_seen
from src.enrich import enrich_missing_abstracts
from src.fetchers.nber import backfill_nber_abstracts
from src.highlight import annotate_all
from src.relevance import is_finance_or_ai_relevant
from src.renderer import render
from src.sources import fetch_source, load_sources
from src.summarizer import SummarizeError, summarize_paper

logger = logging.getLogger("main")

_PROJECT_ROOT = Path(__file__).resolve().parent
PAPERS_PATH = _PROJECT_ROOT / "data" / "papers.json"

_CN_TZ = timezone(timedelta(hours=8))  # 北京时间固定 UTC+8（中国无夏令时）

# 网页改为多页站（首页最新栏 + 每期刊一页，按主题分组），不再单页截断；
# papers.json 始终全量归档。


def load_papers() -> list[dict]:
    """读 data/papers.json 全量归档。不存在/损坏都返回空列表，不报错。"""
    if not PAPERS_PATH.exists():
        logger.info("papers.json 不存在（首次运行？），按空归档处理")
        return []
    try:
        with PAPERS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("papers.json 不是列表，按空归档处理")
            return []
        return data
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning("读取 papers.json 失败（%r），按空归档处理", ex)
        return []


def _sort_key(paper: dict):
    # 与 renderer 一致：按日期倒序，无日期(None)视为最新排最前
    return (paper.get("published_date") or "9999-99-99", str(paper.get("id", "")))


def save_papers(papers: list[dict]) -> None:
    """把全量归档写回 data/papers.json，按日期+id 排序，git diff 干净。"""
    ordered = sorted(papers, key=_sort_key, reverse=True)
    PAPERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PAPERS_PATH.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
    logger.info("已写入 papers.json，共 %d 篇：%s", len(ordered), PAPERS_PATH)


def fetch_all() -> list[dict]:
    """按 config/sources.yaml 抓所有启用的源。
    某个源整体失败只记日志，不影响其他源。"""
    all_papers: list[dict] = []
    for cfg in load_sources():
        name = cfg.get("name", "?")
        try:
            papers = fetch_source(cfg)
            logger.info("源 %s 抓到 %d 篇", name, len(papers))
            all_papers.extend(papers)
        except Exception:
            logger.exception("源 %s 抓取整体失败，跳过该源", name)
    return all_papers


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("========== 流水线开始 ==========")

    today = datetime.now(_CN_TZ).strftime("%Y-%m-%d")

    # 1. 读状态
    seen = load_seen()
    archive = load_papers()
    logger.info("已见指纹 %d 个；归档已有 %d 篇", len(seen), len(archive))

    # 1b. first_seen 创世回填：我们从现在才开始记录"流水线首次抓到本文的日期"。
    #     历史归档没有这个信息，诚实地用各自 published_date 作代理（无则用今天）；
    #     setdefault 幂等，之后只有真正的新论文会被盖上当天的 first_seen。
    backfilled = 0
    for p in archive:
        if not p.get("first_seen"):
            p["first_seen"] = p.get("published_date") or today
            backfilled += 1
    if backfilled:
        logger.info("first_seen 创世回填 %d 篇（用其 published_date 作代理）", backfilled)

    # 2. 抓取
    fetched = fetch_all()
    logger.info("本次共抓取 %d 篇（含新旧）", len(fetched))

    # 3. 过滤出新论文（指纹不在 seen 里；并对本次抓取内部也去重）
    #    MS/NBER 新论文额外过一遍金融/AI 相关性筛选（避免非金融内容进入归档）。
    _RELEVANCE_SOURCES = {"MS", "NBER"}
    new_papers: list[dict] = []
    seen_this_run: set[str] = set()
    skipped_irrelevant = 0
    for paper in fetched:
        h = hash_paper(paper)
        if h in seen or h in seen_this_run:
            continue
        if paper.get("source") in _RELEVANCE_SOURCES:
            if not is_finance_or_ai_relevant(
                paper.get("title", ""),
                paper.get("abstract", ""),
            ):
                skipped_irrelevant += 1
                continue
        seen_this_run.add(h)
        new_papers.append(paper)
    if skipped_irrelevant:
        logger.info("MS/NBER 相关性过滤：跳过 %d 篇非金融论文", skipped_irrelevant)
    logger.info("过滤后新论文 %d 篇", len(new_papers))

    # 冒烟测试限量
    max_new = os.getenv("MAX_NEW_PAPERS")
    if max_new:
        try:
            n = int(max_new)
            if 0 <= n < len(new_papers):
                logger.warning("MAX_NEW_PAPERS=%d 生效，只处理前 %d 篇新论文", n, n)
                new_papers = new_papers[:n]
        except ValueError:
            logger.warning("MAX_NEW_PAPERS=%r 不是整数，忽略", max_new)

    # 4. 缺摘要补全（Semantic Scholar 按 DOI）：
    #    新论文就地补；归档里 abstract_missing 的用【副本】补，
    #    这样补全/重总结失败时绝不污染归档原件（守 never-drop）。
    revivable = [p for p in archive if p.get("abstract_missing")]
    revivable_work = [dict(p) for p in revivable]
    if revivable_work:
        logger.info("归档里 abstract_missing 待复活 %d 篇", len(revivable_work))
    enrich_missing_abstracts(new_papers + revivable_work)

    # 4b. NBER RSS 二次补全：S2 没收录的 NBER 缺摘要论文，尝试从当前 RSS 取摘要。
    #     NBER 有时先发 RSS 再补摘要，延迟可达数天；每次运行顺手重拉一次。
    backfill_nber_abstracts(revivable_work)

    # 5 + 6. 盖日期章 -> 逐篇总结（单篇失败不影响其他）
    newly_added: list[dict] = []
    failed = 0
    for i, paper in enumerate(new_papers, 1):
        if not paper.get("published_date"):
            paper["published_date"] = today  # NBER 首次抓到 -> 盖今天
        paper["first_seen"] = today  # 真正的新论文：盖上"流水线首次抓到日"
        title = paper.get("title", "<无标题>")
        logger.info("[%d/%d] 处理：[%s] %s", i, len(new_papers),
                    paper.get("source"), title[:60])
        try:
            enriched = summarize_paper(paper)
            newly_added.append(enriched)
        except SummarizeError:
            failed += 1
            logger.error("跳过（总结失败，下次会重试）：%s", title[:60])
        except Exception:
            failed += 1
            logger.exception("跳过（未预期错误，下次会重试）：%s", title[:60])

    logger.info("总结完成：成功 %d 篇，失败 %d 篇", len(newly_added), failed)

    # 7. 复活：归档里曾缺摘要、这次被 S2 补上的，重做 AI 总结。
    #    仍补不到的维持原状（下次再试）；重总结失败也不动归档原件。
    revived: list[dict] = []
    revived_fail = 0
    for work in revivable_work:
        if not (work.get("abstract") or "").strip():
            continue  # S2 仍未收录，保持 abstract_missing，留待下次
        title = work.get("title", "<无标题>")
        logger.info("复活（补到摘要，重做总结）：[%s] %s",
                    work.get("source"), title[:60])
        try:
            revived.append(summarize_paper(work))
        except SummarizeError:
            revived_fail += 1
            logger.error("复活总结失败（下次会重试）：%s", title[:60])
        except Exception:
            revived_fail += 1
            logger.exception("复活时未预期错误（下次会重试）：%s", title[:60])
    if revivable_work:
        logger.info("复活完成：成功 %d 篇，失败 %d 篇（其余 S2 仍无、下次再试）",
                    len(revived), revived_fail)

    # 8. 并入归档：归档为底；复活的按同指纹【替换】为新版；
    #    新论文仅当指纹不存在时加入（保住已有条目的原 published_date）。
    by_hash: dict[str, dict] = {}
    for p in archive:
        by_hash.setdefault(hash_paper(p), p)
    for rp in revived:
        by_hash[hash_paper(rp)] = rp           # 复活：原地替换旧的缺摘要版本
    for np in newly_added:
        by_hash.setdefault(hash_paper(np), np)
    all_papers = list(by_hash.values())

    save_papers(all_papers)
    # seen 按"归档里有什么"重算，保证 seen.json 与 papers.json 永远一致、自愈
    save_seen({hash_paper(p) for p in all_papers})

    # 9. 高亮打标：纯函数，对全量归档重算；【不写进 papers.json】，
    #    改 config/highlight.yaml 下次跑即生效，无需重新总结。
    highlighted = annotate_all(all_papers)

    # 9b. "本次新增"标记：first_seen == 本次运行日 = 这轮刷新才抓到的。
    #     运行相关的瞬时标记，只用于渲染，【不持久化】（下次跑自动重算，
    #     旧文 first_seen 不再等于当天 -> 徽章自动消失）。
    n_new = 0
    for p in highlighted:
        p["is_new"] = (p.get("first_seen") == today)
        if p["is_new"]:
            n_new += 1
    logger.info("本次新增（first_seen=%s）%d 篇", today, n_new)

    # 渲染多页静态站（首页最新栏 + 每期刊一页，按主题分组）
    out = render(highlighted)

    logger.info("========== 流水线结束 ==========")
    logger.info("归档总计 %d 篇 | 新增 %d 篇 | 复活 %d 篇 | 失败 %d 篇 | 网页：%s",
                len(all_papers), len(newly_added), len(revived), failed, out)


if __name__ == "__main__":
    main()
