"""抓取 NBER（美国国家经济研究局）最新工作论文。

数据源：https://www.nber.org/rss/new.xml
（注意：旧的 https://www.nber.org/papers.rss 已 404 废弃，不要再用。）

这个 feed 的真实结构很"精简"，写代码时要针对它的特点处理：
  1. 必须带浏览器 User-Agent，否则会被重定向/拦截，feedparser 解析不到内容。
  2. 没有独立的作者字段：title 形如 "论文标题 -- by 作者1, 作者2"，
     需要从 title 里把作者切出来。
  3. 整个 feed 不提供任何日期（实测 entry 只有 id/link/summary/title）。
     真实发布日期只在论文落地页的 <meta citation_publication_date> 里，
     故每篇额外抓一次落地页解析出真日期（精确到天）。取不到才返回 None，
     由 main.py 回退到"首次抓到当天"——不编造、也绝不因此丢文章。
  4. link/id 形如 https://www.nber.org/papers/w35195#fromrss，
     里面的 w35195 是论文号，用它做稳定的 id；url 去掉 #fromrss 片段。

fetch_nber() 是一个无状态纯函数：每次调用只负责"把现在 feed 里有什么抓回来"，
不读写任何文件、不去重、不判断新旧——这些交给上层 main.py，符合模块独立的设计。
"""

import logging
import re
import time
import urllib.request

import feedparser

logger = logging.getLogger(__name__)

# NBER 当前有效的"最新工作论文"RSS 地址（全学科混合，未来再按金融过滤）
NBER_FEED_URL = "https://www.nber.org/rss/new.xml"

# 用浏览器 UA：实测默认的 Python/feedparser UA 会被 NBER 重定向后拦掉，拿到 0 条
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# title 里标题和作者的分隔符，例如：
#   "AlphaPortfolio: ... -- by Lin William Cong, Ke Tang, Jingyuan Wang"
_TITLE_AUTHOR_SEP = " -- by "

# 从链接里抽取 NBER 论文号，例如 .../papers/w35195#fromrss -> w35195
_PAPER_ID_RE = re.compile(r"/papers/([a-z]\d+)", re.IGNORECASE)

# NBER 落地页里的 Google Scholar meta，含真实发布日期（实测精确到天）：
#   <meta name="citation_publication_date" content="2026/05/18" />
_PUB_DATE_META_RE = re.compile(
    r'name=["\']citation_publication_date["\']'
    r'[^>]*content=["\'](\d{4})[/-](\d{1,2})[/-](\d{1,2})["\']',
    re.IGNORECASE,
)
# content 与 name 顺序可能反过来，再来一个反序版
_PUB_DATE_META_RE2 = re.compile(
    r'content=["\'](\d{4})[/-](\d{1,2})[/-](\d{1,2})["\']'
    r'[^>]*name=["\']citation_publication_date["\']',
    re.IGNORECASE,
)


def _fetch_real_date(url: str) -> str | None:
    """抓 NBER 论文落地页，解析真实发布日期 -> 'YYYY-MM-DD'。

    RSS feed 完全不含日期，旧逻辑一律盖"今天" -> 把早几周发布的论文
    伪造成"今天的新论文"（这是用户反馈的严重错误信息）。真日期只在
    落地页 meta 里，故每篇多抓一次该页。

    任何失败（网络/超时/页面无该 meta/格式异常）都返回 None，
    由 main.py 回退到"首次抓到当天"——绝不因取日期失败而丢文章。
    """
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html_text = r.read().decode("utf-8", "replace")
    except Exception as ex:
        logger.warning("取 NBER 落地页失败（%s）：%r，日期回退", url, ex)
        return None

    m = _PUB_DATE_META_RE.search(html_text) or _PUB_DATE_META_RE2.search(html_text)
    if not m:
        logger.warning("NBER 落地页未找到 citation_publication_date：%s，日期回退", url)
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return "%04d-%02d-%02d" % (y, mo, d)
    except (TypeError, ValueError):
        return None


def _split_title_and_authors(raw_title: str) -> tuple[str, list[str]]:
    """把 "标题 -- by 作者1, 作者2" 拆成 (干净标题, 作者列表)。

    用 rsplit 从右边切一次，避免标题本身含 ' -- by ' 时切错。
    没有 ' -- by ' 时，原样返回标题、作者列表为空。
    """
    if _TITLE_AUTHOR_SEP in raw_title:
        title_part, authors_part = raw_title.rsplit(_TITLE_AUTHOR_SEP, 1)
        authors = [a.strip() for a in authors_part.split(",") if a.strip()]
        return title_part.strip(), authors
    return raw_title.strip(), []


def _extract_paper_id(link: str) -> str:
    """从论文链接里抽出 NBER 论文号（如 w35195）当作稳定 id。

    抽不到时退回用去掉 #片段 的链接本身，保证 id 一定有值。
    """
    match = _PAPER_ID_RE.search(link or "")
    if match:
        return match.group(1).lower()
    return (link or "").split("#")[0]


def fetch_nber() -> list[dict]:
    """抓取 NBER 最新工作论文，返回 list[dict]。

    每个 dict 的字段（统一接口，JF 等其他源也照这个来）：
      - id:             str   论文号，如 "w35195"
      - title:          str   干净标题（已去掉 ' -- by 作者' 后缀）
      - authors:        list  作者名列表
      - abstract:       str   摘要全文
      - url:            str   论文页链接（已去掉 #fromrss）
      - published_date: str|None  落地页 meta 解析的真日期；取不到为 None 由 main.py 盖今天
      - source:         str   固定 "NBER"

    任何抓取/解析失败都不抛异常，而是记日志并返回已成功解析的部分
    （单源失败不该拖垮整个流程，这点和 main.py 的容错策略一致）。
    """
    logger.info("开始抓取 NBER：%s", NBER_FEED_URL)
    parsed = feedparser.parse(NBER_FEED_URL, agent=BROWSER_UA)

    # bozo=1 表示 feedparser 觉得内容不太规范，但常常仍能解析出 entries，
    # 所以只当作警告，真正以"能不能拿到 entries"为准。
    if parsed.bozo:
        logger.warning("NBER feed 解析有警告：%r", parsed.get("bozo_exception"))

    entries = parsed.entries or []
    if not entries:
        logger.error(
            "NBER feed 没有解析到任何条目（HTTP status=%s）", parsed.get("status")
        )
        return []

    papers: list[dict] = []
    for entry in entries:
        try:
            raw_title = (entry.get("title") or "").strip()
            link = entry.get("link") or entry.get("id") or ""
            if not raw_title or not link:
                logger.warning("跳过一条缺标题或链接的 NBER 条目：%r", entry.get("id"))
                continue

            title, authors = _split_title_and_authors(raw_title)
            abstract = (entry.get("summary") or "").strip()
            url = link.split("#")[0]  # 去掉 #fromrss 片段

            real_date = _fetch_real_date(url)  # 取不到 -> None -> main.py 盖今天
            time.sleep(0.4)  # 礼貌间隔，避免连发把 NBER 惹毛

            papers.append(
                {
                    "id": _extract_paper_id(link),
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "url": url,
                    "published_date": real_date,
                    "source": "NBER",
                }
            )
        except Exception:  # 单条出错不影响其他条目
            logger.exception("解析某条 NBER 条目时出错，已跳过")

    logger.info("NBER 抓取完成，成功解析 %d 篇", len(papers))
    return papers


if __name__ == "__main__":
    # 直接运行本文件时的快速自测：python -m src.fetchers.nber
    # 配一个会输出到控制台的 logger，方便看过程。
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    result = fetch_nber()
    print(f"\n共抓到 {len(result)} 篇，前 3 篇预览：\n")
    for p in result[:3]:
        print("id      :", p["id"])
        print("title   :", p["title"])
        print("authors :", p["authors"])
        print("url     :", p["url"])
        print("pub_date:", p["published_date"])
        print("abstract:", p["abstract"][:200], "...")
        print("-" * 70)
