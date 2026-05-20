# [DeepSeek-Patch: SSRN fetcher rewritten to use OpenAlex API]
import logging
import json
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# [DeepSeek-Patch: OpenAlex constants]
SSRN_SOURCE_ID = "S4210172589"
OPENALEX_BASE = "https://api.openalex.org/works"

# [DeepSeek-Patch: helper to rebuild abstract from inverted index]
def _rebuild_abstract(inv: dict | None) -> str:
    """Reconstruct abstract text from OpenAlex inverted index."""
    if not inv:
        return ''
    pos_word = {}
    for word, positions in inv.items():
        for p in positions:
            pos_word[p] = word
    return ' '.join(pos_word[i] for i in sorted(pos_word))

# [DeepSeek-Patch: fetch_ssrn function using OpenAlex API]
def fetch_ssrn(watch_authors: list[dict], source_name: str = "SSRN") -> list[dict]:
    """Fetch SSRN papers via OpenAlex API for each author, deduplicate by DOI/URL."""
    if not watch_authors:
        logger.info("SSRN: watch_authors 为空，跳过")
        return []

    seen: set[str] = set()
    articles: list[dict] = []

    for author in watch_authors:
        name = author.get("name", "")
        openalex_id = author.get("openalex_id")
        if not openalex_id:
            logger.warning("SSRN: 作者 %s 缺少 openalex_id，跳过", name)
            continue

        # Build query URL
        query = (
            f"{OPENALEX_BASE}"
            f"?filter=primary_location.source.id:{SSRN_SOURCE_ID}"
            f",authorships.author.id:{openalex_id}"
            f"&per_page=20&sort=publication_date:desc"
            f"&select=id,title,doi,publication_date,authorships,abstract_inverted_index,primary_location,ids"
        )
        logger.info("SSRN: 正在抓取 %s (openalex_id=%s)", name, openalex_id)

        req = urllib.request.Request(
            query,
            headers={
                "User-Agent": "finance-digest-bot/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
            logger.warning("SSRN: 抓取 %s 失败: %s", name, exc)
            continue

        results = data.get("results", [])
        for item in results:
            # Dedup key: doi (preferred) or url
            doi_raw = item.get("doi") or (item.get("ids") or {}).get("doi") or ""
            doi = doi_raw.lstrip("https://doi.org/")
            primary_loc = item.get("primary_location") or {}
            url = primary_loc.get("landing_page_url") or (item.get("ids") or {}).get("openalex", "")
            dedup_key = doi if doi else url
            if not dedup_key or dedup_key in seen:
                continue
            seen.add(dedup_key)

            title = item.get("title") or ""
            published_date = item.get("publication_date")  # already YYYY-MM-DD or None

            # Authors
            authorships = item.get("authorships") or []
            authors = []
            for a in authorships:
                author_obj = a.get("author") or {}
                display_name = author_obj.get("display_name")
                if display_name:
                    authors.append(display_name)

            # Abstract
            abstract = _rebuild_abstract(item.get("abstract_inverted_index"))

            articles.append({
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "url": url,
                "published_date": published_date,
                "source": source_name,
                "doi": doi,
            })

    logger.info("SSRN: 共获取 %d 篇论文", len(articles))
    return articles
