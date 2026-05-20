"""通用 Crossref API fetcher（用于 RSS 烂/被封的期刊：JFE / MS / RFS）。

接口：fetch_crossref(issn, source, rows=40) -> list[dict]，7 字段统一 schema。

为什么用 Crossref：实测它对这三个期刊都稳定提供【作者 + 日期 + DOI】
（解决了它们 RSS 无作者 / RFS 被反爬封的问题）。摘要看出版商是否上传：
  - MS / RFS：有摘要（JATS XML，需去标签；RFS 还带 "Abstract" 前缀）
  - JFE：Elsevier 不存摘要 -> abstract 为 ""，按"绝不丢文章"原则保留，
         由 summarizer 如实标注并跳过 AI 总结。

只取最近 rows 篇 journal-article，按发表时间倒序；去重让重复运行很便宜
（已见过的不会再总结）。
"""

import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from src.textnoise import is_noise_title

_CN_TZ = timezone(timedelta(hours=8))  # 与 main.py 一致，UTC+8 判定"今天"

logger = logging.getLogger(__name__)

_API = "https://api.crossref.org/journals/{issn}/works?{q}"
# Crossref 礼貌池建议带联系方式；这里用项目标识（不放个人邮箱）
_UA = "finance-papers-digest/1.0 (academic personal digest; +https://github.com/)"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ABSTRACT_PREFIX_RE = re.compile(r"^\s*abstract\s*:?\s*", re.IGNORECASE)

# 卷首页/通告识别统一在 src.textnoise（跨 Crossref/Wiley 单一来源）。
# Crossref 没有 Wiley 的 prism_section 字段，只能靠标题判定。


def _clean_abstract(raw: str) -> str:
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)        # 去 JATS/HTML 标签
    text = html.unescape(text)
    text = _ABSTRACT_PREFIX_RE.sub("", text)  # 去开头 "Abstract"
    return " ".join(text.split()).strip()


def _authors(item: dict) -> list[str]:
    out = []
    for a in item.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        full = f"{given} {family}".strip()
        if not full:
            full = (a.get("name") or "").strip()  # 机构作者
        if full:
            out.append(full)
    return out


def _parts_to_str(item: dict, key: str) -> str | None:
    """item[key].date-parts -> 'YYYY-MM-DD'（缺月/日补 01）。零填充后字符串
    比较即等价于时间比较，可直接和 'today' 比大小。"""
    parts = (item.get(key) or {}).get("date-parts") or []
    if parts and parts[0] and parts[0][0]:
        ymd = (list(parts[0]) + [1, 1])[:3]
        try:
            return "%04d-%02d-%02d" % (ymd[0], ymd[1], ymd[2])
        except (TypeError, ValueError):
            return None
    return None


def _date(item: dict) -> str | None:
    """选"文章真正可用日"，且【永不返回未来日期】。

    坑：Elsevier 等把 published / published-print / issued 填成【未来的纸刊
    封面期号日】（如 2026-08），但文章其实早已 online 先发。Crossref 的
    `created`（建档日）才≈真实上线日。规则：
      1. 有 published-online 且≤今天 -> 用它（最准的真实上线日）
      2. 否则 published/印刷/issued 里≤今天的 -> 用它（正常历史文照旧不受影响）
      3. 名义日都是未来（或缺失）-> 回退 created（≈真实上线日）
      4. created 也异常 -> None（宁缺勿造未来日）
    """
    today = datetime.now(_CN_TZ).strftime("%Y-%m-%d")

    online = _parts_to_str(item, "published-online")
    if online and online <= today:
        return online

    for key in ("published", "published-print", "issued"):
        d = _parts_to_str(item, key)
        if d and d <= today:
            return d

    created = _parts_to_str(item, "created")
    if created and created <= today:
        return created
    return created or None  # 极端兜底：仍优先 created，绝不臆造


def fetch_crossref(issn: str, source: str, rows: int = 40) -> list[dict]:
    q = urllib.parse.urlencode({
        "filter": "type:journal-article",
        "sort": "published",
        "order": "desc",
        "rows": str(max(1, min(int(rows), 200))),
        "select": ("title,author,abstract,published,published-online,"
                   "published-print,issued,created,DOI,URL"),
    })
    url = _API.format(issn=issn, q=q)
    logger.info("开始抓取 %s（Crossref ISSN=%s, rows=%s）", source, issn, rows)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
    except Exception as ex:
        logger.error("%s Crossref 请求失败：%r", source, ex)
        return []

    items = data.get("message", {}).get("items", []) or []
    papers: list[dict] = []
    no_abstract = 0
    skipped_noise = 0
    for it in items:
        try:
            titles = it.get("title") or []
            title = (titles[0] if titles else "").strip()
            doi = (it.get("DOI") or "").strip()
            if not title or not doi:
                continue
            if is_noise_title(title):
                skipped_noise += 1
                logger.info("%s 跳过卷首页/通告（非论文）：%s", source, title[:70])
                continue

            abstract = _clean_abstract(it.get("abstract") or "")
            if not abstract:
                no_abstract += 1  # 不丢弃，保留、由 summarizer 如实标注

            papers.append(
                {
                    "id": doi,
                    "title": title,
                    "authors": _authors(it),
                    "abstract": abstract,
                    "url": it.get("URL") or f"https://doi.org/{doi}",
                    "published_date": _date(it),
                    "source": source,
                }
            )
        except Exception:
            logger.exception("%s 解析某条 Crossref 记录出错，已跳过", source)

    if skipped_noise:
        logger.info("%s 共跳过 %d 条卷首页/通告（标题 denylist）", source, skipped_noise)
    if no_abstract:
        logger.warning(
            "%s 有 %d 篇 Crossref 无摘要（如 Elsevier 不存摘要），"
            "已保留并将如实标注（未丢弃）", source, no_abstract)
    logger.info("%s 抓取完成，成功解析 %d 篇", source, len(papers))
    return papers


_NBER_ID_RE = re.compile(r"/(?:10\.3386/)?([a-z]\d+)$", re.IGNORECASE)


def _item_to_paper(it: dict, source: str, relevance=None) -> dict | None:
    """Crossref 单条 -> 统一 schema dict。返回 None 表示该条被丢弃
    （缺标题/DOI、卷首页噪音、或未通过相关性预筛）。"""
    titles = it.get("title") or []
    title = (titles[0] if titles else "").strip()
    doi = (it.get("DOI") or "").strip()
    if not title or not doi:
        return None
    if is_noise_title(title):
        return None

    abstract = _clean_abstract(it.get("abstract") or "")

    # 相关性预筛（仅 MS/NBER 传入；金融5 不传 -> 全收）
    if relevance is not None and not relevance(
        title, abstract, it.get("subject") or []
    ):
        return None

    # NBER 走 prefix 端点，DOI 形如 10.3386/w35221 -> id 归一成 w35221，
    # 与 RSS 来源同形，靠 title+作者的指纹去重时不会重复。
    pid = doi
    if source == "NBER":
        m = _NBER_ID_RE.search(doi)
        if m:
            pid = m.group(1).lower()

    return {
        "id": pid,
        "title": title,
        "authors": _authors(it),
        "abstract": abstract,
        "url": it.get("URL") or f"https://doi.org/{doi}",
        "published_date": _date(it),
        "source": source,
    }


def fetch_crossref_backfill(
    source: str,
    issn: str | None = None,
    prefix: str | None = None,
    from_created: str = "2022-11-30",
    relevance=None,
    page_rows: int = 200,
    max_items: int | None = None,
) -> list[dict]:
    """历史回填：Crossref 游标深翻，从 from_created（建档日，≈真实上线日，
    避开 Elsevier 未来封面日）起的全部记录。

    issn：按期刊 ISSN（金融5）；prefix：按 DOI 前缀（NBER=10.3386）。
    relevance：可选 (title, abstract, subjects)->bool 预筛（仅 MS/NBER 传）。
    任一页失败只记日志并返回已抓到的部分（绝不让回填整体崩）。
    """
    if issn:
        base = f"https://api.crossref.org/journals/{issn}/works"
        filt = f"type:journal-article,from-created-date:{from_created}"
    elif prefix:
        base = f"https://api.crossref.org/prefixes/{prefix}/works"
        filt = f"from-created-date:{from_created}"  # NBER 类型不是 journal-article
    else:
        raise ValueError("issn 与 prefix 必须给一个")

    select = ("title,author,abstract,subject,published,published-online,"
              "published-print,issued,created,DOI,URL")
    rows = max(1, min(int(page_rows), 1000))
    papers: list[dict] = []
    dropped = 0
    logger.info("开始回填 %s（from-created-date≥%s, %s）",
                source, from_created, issn or prefix)

    # 先取 total-results 算页数（Crossref offset 上限 10000，我们各源都 < 此）
    def _get(url: str) -> dict | None:
        for attempt in (1, 2, 3, 4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.load(r).get("message", {})
            except Exception as ex:
                if attempt == 4:
                    logger.error("%s 请求连试 4 次失败：%r", source, ex)
                    return None
                logger.warning("%s 请求第 %d 次失败：%r，退避重试",
                                source, attempt, ex)
                time.sleep(2 * attempt)
        return None

    head = _get(f"{base}?" + urllib.parse.urlencode(
        {"filter": filt, "rows": "0"}))
    total = (head or {}).get("total-results") or 0
    hard_cap = 10000  # Crossref offset 硬上限
    if total > hard_cap:
        logger.warning("%s 命中 %d 条，超过 Crossref offset 上限 %d，只能取前 %d",
                        source, total, hard_cap, hard_cap)
    limit = min(total, hard_cap)
    logger.info("%s 命中 %d 条，按每页 %d、offset 分页抓取", source, total, rows)

    # offset 分页：每页独立请求，某页失败只跳过该页、继续后面（不连累整体）
    offset = 0
    page = 0
    while offset < limit:
        url = f"{base}?" + urllib.parse.urlencode({
            "filter": filt, "rows": str(rows), "offset": str(offset),
            "select": select,
        })
        msg = _get(url)
        page += 1
        if msg is None:
            logger.error("%s 第 %d 页(offset=%d)放弃，跳过继续后面",
                          source, page, offset)
            offset += rows
            continue

        for it in msg.get("items") or []:
            try:
                p = _item_to_paper(it, source, relevance)
                if p is None:
                    dropped += 1
                else:
                    papers.append(p)
            except Exception:
                logger.exception("%s 回填解析某条出错，已跳过", source)

        logger.info("%s 回填进度：第 %d 页(offset=%d)，累计保留 %d / 丢弃 %d",
                    source, page, offset, len(papers), dropped)
        if max_items and len(papers) >= max_items:
            papers = papers[:max_items]
            break
        offset += rows
        time.sleep(0.4)  # 礼貌间隔

    logger.info("%s 回填完成：命中 %d，保留 %d，按规则丢弃 %d",
                source, total, len(papers), dropped)
    return papers


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    _issn = sys.argv[1] if len(sys.argv) > 1 else "1465-7368"   # RFS
    _src = sys.argv[2] if len(sys.argv) > 2 else "RFS"
    res = fetch_crossref(_issn, _src, rows=6)
    print(f"\n{_src} 共 {len(res)} 篇，前 3：\n")
    for p in res[:3]:
        print(p["id"], "|", p["title"][:60])
        print("  authors:", p["authors"][:4], "| date:", p["published_date"])
        print("  abstract:", (p["abstract"][:160] or "【无】"))
