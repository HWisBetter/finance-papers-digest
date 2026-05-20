# [DeepSeek-Patch: SSRN author RSS fetcher]
import logging
import re
from datetime import datetime

import feedparser
import requests

logger = logging.getLogger(__name__)

# [DeepSeek-Patch: fetch_ssrn function]
def fetch_ssrn(watch_authors: list[dict], source_name: str = "SSRN") -> list[dict]:
    """Fetch SSRN RSS feed for each author, deduplicate by URL, return article list."""
    if not watch_authors:
        logger.info("SSRN: watch_authors 为空，跳过")
        return []

    seen_urls: set[str] = set()
    articles: list[dict] = []

    for author in watch_authors:
        name = author.get("name", "")
        ssrn_id = author.get("ssrn_id")
        if not ssrn_id:
            logger.warning("SSRN: 作者 %s 缺少 ssrn_id，跳过", name)
            continue

        url = f"https://papers.ssrn.com/sol3/Rss/rssfeed.cfm?per_id={ssrn_id}"
        logger.info("SSRN: 正在抓取 %s (id=%s)", name, ssrn_id)

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; finance-digest-bot/1.0)"},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("SSRN: 抓取 %s 失败: %s", name, exc)
            continue

        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)

            title = entry.get("title", "").strip()
            raw_summary = entry.get("summary", "")
            abstract = re.sub(r"<[^>]+>", "", raw_summary).strip()

            # published date
            published_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published_date = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass

            # authors
            raw_author = entry.get("author", "")
            if raw_author:
                authors = [
                    a.strip()
                    for a in re.split(r"\s*(?:and|,)\s*", raw_author)
                    if a.strip()
                ]
            else:
                authors = [name] if name else []

            # doi
            doi = ""
            dc_id = entry.get("dc_identifier", "")
            if dc_id:
                m = re.search(r"10\.\S+", dc_id)
                if m:
                    doi = m.group()

            articles.append(
                {
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "url": link,
                    "published_date": published_date,
                    "source": source_name,
                    "doi": doi,
                }
            )

    logger.info("SSRN: 共获取 %d 篇论文", len(articles))
    return articles
