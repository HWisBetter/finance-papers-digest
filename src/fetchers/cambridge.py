"""通用 Cambridge Core fetcher（JFQA 等）。

接口：fetch_cambridge(feed_url, source) -> list[dict]，7 字段统一 schema。

Cambridge feed 实测特点（与 Wiley 不同，针对性处理）：
  1. 作者是 'Family, Given' 顺序（如 'Pederzoli, Paola'），需转成
     'Given Family'（'Paola Pederzoli'）以和别处的英文自然语序一致，
     大佬高亮匹配才不会错位。多作者取 entry.authors 列表。
  2. 没有 content 字段，摘要在 summary 里（可能含 HTML / "Abstract" 前缀）。
  3. 日期优先用 updated_parsed（struct_time，稳），退路 prism_publicationdate。
  4. id 用 DOI；url 去查询串。
  5. 同样遵守"无摘要也不丢"原则（保留、由 summarizer 如实标注）。
"""

import html
import logging
import re
import time

import feedparser

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ABSTRACT_PREFIX_RE = re.compile(r"^\s*abstract\s*:?\s*", re.IGNORECASE)


def _to_given_family(name: str) -> str:
    """'Pederzoli, Paola' -> 'Paola Pederzoli'；没有逗号则原样。"""
    name = " ".join((name or "").split())
    if "," in name:
        family, given = name.split(",", 1)
        return f"{given.strip()} {family.strip()}".strip()
    return name


def _clean_authors(entry) -> list[str]:
    raw = [a.get("name", "") for a in (entry.get("authors") or []) if a.get("name")]
    if not raw and entry.get("author"):
        raw = [entry["author"]]
    return [n for n in (_to_given_family(x) for x in raw) if n]


def _extract_abstract(entry) -> str:
    text = entry.get("summary", "") or ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _ABSTRACT_PREFIX_RE.sub("", text)
    return " ".join(text.split()).strip()


def _format_date(entry) -> str | None:
    parsed = entry.get("updated_parsed") or entry.get("published_parsed")
    if parsed:
        return time.strftime("%Y-%m-%d", parsed)
    raw = (entry.get("prism_publicationdate") or "").strip()
    # 形如 2026-05-01 的就直接用前 10 位
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return None


def fetch_cambridge(feed_url: str, source: str) -> list[dict]:
    logger.info("开始抓取 %s（Cambridge）：%s", source, feed_url)
    parsed = feedparser.parse(feed_url, agent=BROWSER_UA)

    if parsed.bozo:
        logger.warning("%s feed 解析有警告：%r", source, parsed.get("bozo_exception"))

    entries = parsed.entries or []
    if not entries:
        logger.error("%s feed 无任何条目（HTTP status=%s）",
                     source, parsed.get("status"))
        return []

    papers: list[dict] = []
    kept_no_abstract = 0
    for entry in entries:
        try:
            title = (entry.get("title") or "").strip()
            link = entry.get("link") or entry.get("prism_url") or ""
            if not title or not link:
                logger.warning("%s 跳过缺标题/链接条目：%r", source, entry.get("id"))
                continue

            abstract = _extract_abstract(entry)
            if not abstract:
                kept_no_abstract += 1

            paper_id = entry.get("prism_doi") or entry.get("id") or link
            papers.append(
                {
                    "id": paper_id,
                    "title": title,
                    "authors": _clean_authors(entry),
                    "abstract": abstract,
                    "url": link.split("?")[0],
                    "published_date": _format_date(entry),
                    "source": source,
                }
            )
        except Exception:
            logger.exception("%s 解析某条出错，已跳过", source)

    if kept_no_abstract:
        logger.warning("%s 有 %d 篇无摘要，已保留并将如实标注（未丢弃）",
                       source, kept_no_abstract)
    logger.info("%s 抓取完成，成功解析 %d 篇", source, len(papers))
    return papers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    URL = ("https://www.cambridge.org/core/rss/product/id/"
           "FB35548FF614F4556E96D01FA2CB412E")
    res = fetch_cambridge(URL, "JFQA")
    print(f"\nJFQA 共 {len(res)} 篇，前 3：\n")
    for p in res[:3]:
        print(p["id"], "|", p["title"][:60])
        print("  authors:", p["authors"], "| date:", p["published_date"])
        print("  abstract:", (p["abstract"][:160] or "【无】"))
