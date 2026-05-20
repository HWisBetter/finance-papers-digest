"""通用 Wiley fetcher：Wiley Online Library 的期刊 feed 结构都一样，
只是 ISSN 不同。JF、JAR 等都复用这一个函数（传 ISSN + 来源名）。

接口：fetch_wiley(issn, source) -> list[dict]，7 字段统一 schema。
（这段逻辑原来写在 jf.py，现抽成通用；jf.py 改为薄封装。）

Wiley feed 的特点（实测，JF/JAR 一致）：
  1. title 干净，不含作者。
  2. 作者在 author 字段，挤成一个全大写、夹换行的串，需切分+规范化。
  3. 真摘要在 content[0].value，开头常带 "ABSTRACT"，可能含 HTML。
  4. 有真实发表日期 published_parsed。
  5. id 用 DOI；url 去掉 ?af=R 查询串。
  6. feed 混入非论文卷首/行政条目，用 prism_section 黑名单剔除；
     过了栏目过滤但没摘要的正式文章【绝不丢弃】（保留、由 summarizer
     如实标注并跳过 AI 总结）。
"""

import html
import logging
import re
import time

import feedparser

from src.textnoise import is_noise_title

logger = logging.getLogger(__name__)

WILEY_FEED_TMPL = "https://onlinelibrary.wiley.com/feed/{issn}/most-recent"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 非论文卷首/行政栏目黑名单（比白名单安全：白名单漏掉合法栏目会静默丢真论文）
_SKIP_SECTIONS = {
    "issue information",
    "announcement",
    "announcements",
    "annual meeting minutes",
    "front matter",
    "back matter",
    "editorial board",
    "masthead",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ABSTRACT_PREFIX_RE = re.compile(r"^\s*abstract\s*:?\s*", re.IGNORECASE)


def _clean_authors(author_str: str) -> list[str]:
    """'SVETLANA BRYZGALOVA, \\nJIANTAO HUANG' -> ['Svetlana Bryzgalova', ...]"""
    if not author_str:
        return []
    authors = []
    for part in author_str.split(","):
        name = " ".join(part.split())
        if name:
            authors.append(name.title())
    return authors


def _extract_abstract(entry) -> str:
    text = ""
    content = entry.get("content")
    if content:
        text = content[0].get("value", "") or ""
    if not text:
        text = entry.get("summary", "") or ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _ABSTRACT_PREFIX_RE.sub("", text)
    text = " ".join(text.split())
    return text.strip()


def _format_date(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return time.strftime("%Y-%m-%d", parsed)
    return None


def fetch_wiley(issn: str, source: str) -> list[dict]:
    """抓取某个 Wiley 期刊最新论文。issn 不带连字符（如 JF=15406261）。"""
    url = WILEY_FEED_TMPL.format(issn=issn)
    logger.info("开始抓取 %s（Wiley）：%s", source, url)
    parsed = feedparser.parse(url, agent=BROWSER_UA)

    if parsed.bozo:
        logger.warning("%s feed 解析有警告：%r", source, parsed.get("bozo_exception"))

    entries = parsed.entries or []
    if not entries:
        logger.error("%s feed 无任何条目（HTTP status=%s）",
                     source, parsed.get("status"))
        return []

    papers: list[dict] = []
    kept_no_abstract = 0
    skipped_section = 0
    for entry in entries:
        try:
            title = (entry.get("title") or "").strip()
            link = entry.get("link") or ""
            if not title or not link:
                logger.warning("%s 跳过缺标题/链接条目：%r", source, entry.get("id"))
                continue

            section = (entry.get("prism_section") or "").strip().lower()
            # 双保险：prism_section 黑名单 + 标题兜底（撤稿通告等 prism_section
            # 归不进黑名单，但标题能识别；与 Crossref 用同一套 textnoise 规则）。
            if section in _SKIP_SECTIONS or is_noise_title(title):
                skipped_section += 1
                logger.info("%s 跳过非论文条目：%s", source, title[:70])
                continue

            abstract = _extract_abstract(entry)
            if not abstract:
                kept_no_abstract += 1  # 不丢弃，保留、由 summarizer 如实标注

            paper_id = entry.get("id") or entry.get("prism_doi") or link
            papers.append(
                {
                    "id": paper_id,
                    "title": title,
                    "authors": _clean_authors(entry.get("author") or ""),
                    "abstract": abstract,
                    "url": link.split("?")[0],
                    "published_date": _format_date(entry),
                    "source": source,
                }
            )
        except Exception:
            logger.exception("%s 解析某条出错，已跳过", source)

    if skipped_section:
        logger.info("%s 跳过 %d 条非论文栏目条目", source, skipped_section)
    if kept_no_abstract:
        logger.warning("%s 有 %d 篇正式文章无摘要，已保留并将如实标注（未丢弃）",
                       source, kept_no_abstract)
    logger.info("%s 抓取完成，成功解析 %d 篇", source, len(papers))
    return papers


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    _issn = sys.argv[1] if len(sys.argv) > 1 else "15406261"
    _src = sys.argv[2] if len(sys.argv) > 2 else "JF"
    res = fetch_wiley(_issn, _src)
    print(f"\n{_src} 共 {len(res)} 篇，前 3：\n")
    for p in res[:3]:
        print(p["id"], "|", p["title"][:60])
        print("  authors:", p["authors"], "| date:", p["published_date"])
        print("  abstract:", (p["abstract"][:150] or "【无】"))
