"""把（已总结+已打标）论文渲染成一个【多页静态站】到 docs/。

产物：
  docs/index.html         首页：🆕 最新收录一批 + 一条分隔线 + 各期刊入口
  docs/<SOURCE>.html       每个期刊一页：该刊全部论文，按 topic 主题分组
  docs/style.css           共享样式（源码里维护的静态文件，渲染器不覆盖它）

接口：render(papers: list[dict], output_dir: Path|None=None) -> Path
  返回 index.html 路径。papers 里每个 dict 至少含：
    title/authors/url/published_date/source/summary/keywords/ai_comment
  可选：topic / first_seen / is_new / is_ai / is_featured / is_retracted ...

设计要点：
  1. 模板用 Jinja2 继承：base.html.j2 + _macros.html.j2，改样式只动 style.css。
  2. autoescape=True，标题/作者里的 < & " 自动转义，防注入/防破版。
  3. 不再单页截断（原 RENDER_LIMIT 取消）——靠"分期刊 + 分主题"组织，
     规模大也井井有条；首页只放最新一批 + 期刊入口，不堆全部。
  4. 排序：期刊页组内按 published_date 倒序；首页"最新"= first_seen 最大的那批。
  5. 全静态、CSS 外链同目录 style.css，直接可部署 GitHub Pages。
"""

import logging
import re as _re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.highlight import _normalize, load_highlight_config
from src.leaderboard import active_top_n

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_DOCS_DIR = _PROJECT_ROOT / "docs"

_CN_TZ = timezone(timedelta(hours=8))  # 北京时间固定 UTC+8，无夏令时

# 期刊在导航/首页里的固定展示顺序（金融核心在前，宽口径源靠后）；
# 未列出的源按字母补在最后，保证新增源也不会丢。
_JOURNAL_ORDER = ["JF", "JFE", "RFS", "JFQA", "JAR", "NBER", "MS"]
# SSRN 不单独出页面，其论文全部收入「大佬前瞻」
_NAV_EXCLUDE = {"SSRN"}

_UNTOPIC = "未分类"  # topic 字段还没回填时的兜底主题（阶段1b 后会被真主题替换）

# 「大佬前瞻」：跨期刊把命中关注学者(is_featured)的论文聚到一起，按大佬分组。
# 现为已发表论文；后续大佬名单扩充 + 接入 SSRN 后再纳入其 working paper。
FEATURED_NAME = "大佬前瞻"
_FEATURED_HREF = "featured.html"

# 「发文活跃榜」：数据驱动排行，独立页面。
_LEADERBOARD_NAME = "活跃榜"
_LEADERBOARD_HREF = "leaderboard.html"
_LEADERBOARD_N = 30  # 展示条数（比首页 Top-10 更完整，可滚动）


def _date_key(paper: dict) -> str:
    """published_date 形如 'YYYY-MM-DD'，ISO 串可直接按字典序倒排；
    None（理论上已很少）用 '0000-00-00' 垫底，不顶到最前误导。"""
    return paper.get("published_date") or "0000-00-00"


def _journal_order_index(source: str) -> tuple:
    if source in _JOURNAL_ORDER:
        return (0, _JOURNAL_ORDER.index(source))
    return (1, source)  # 未登记的源：排在已登记之后，按名字稳定排序


def _group_by_topic(papers: list[dict]) -> list[dict]:
    """按 topic 分组；组内按发表日期倒序；'未分类' 永远排最后。"""
    buckets: dict[str, list] = defaultdict(list)
    for p in papers:
        topic = (p.get("topic") or "").strip() or _UNTOPIC
        buckets[topic].append(p)

    def grp_key(item):
        topic, ps = item
        # 未分类置底；其余按组内篇数多→少，篇数相同按主题名稳定排序
        return (topic == _UNTOPIC, -len(ps), topic)

    groups = []
    for topic, ps in sorted(buckets.items(), key=grp_key):
        groups.append({
            "topic": topic,
            "papers": sorted(ps, key=_date_key, reverse=True),
        })
    return groups


def _canon(name: str, alias_norm: dict[str, str]) -> str:
    """同一人多写法 -> 规范显示名（仅展示/分组用；匹配仍用 highlight 全名单）。
    alias_norm: {归一化变体名: 规范显示名}。命不中则原样返回。"""
    return alias_norm.get(_normalize(name or ""), name)


def _group_by_author(papers: list[dict],
                     alias_norm: dict[str, str] | None = None) -> list[dict]:
    """「大佬前瞻」按命中的关注学者分组；一篇命中多位则各组都出现。
    分组键用【规范显示名】，使 "Bryan Kelly"/"Bryan T. Kelly" 合到一组。"""
    alias_norm = alias_norm or {}
    buckets: dict[str, list] = defaultdict(list)
    for p in papers:
        seen_here: set[str] = set()
        for a in (p.get("featured_matched") or ["关注作者"]):
            ca = _canon(a, alias_norm)
            if ca in seen_here:   # 同篇命中同一人的两种写法 -> 只计一次
                continue
            seen_here.add(ca)
            buckets[ca].append(p)
    groups = []
    for author, ps in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        groups.append({
            "author": author,
            "papers": sorted(ps, key=_date_key, reverse=True),
        })
    return groups


def _featured_roster(groups: list[dict],
                     alias_norm: dict[str, str] | None = None) -> list[dict]:
    """config/highlight.yaml 里的【大佬名单】-> 页面顶部 chip 条数据。

    名单里的名字先按 alias 规范化、再按归一化对上分组锚点：
    对上 -> 可点跳转 + 篇数；没对上(暂无命中论文) -> 灰色不可点，但仍展示
    （让用户清楚名单里有谁）。规范化后去重 -> 同一人只显示一个 chip
    （"Bryan Kelly"/"Bryan T. Kelly" 合并为一个），避免用户误以为是两个人。
    锚点序号 = 分组在 groups 中的位置+1，与 featured.html.j2 的 loop.index 对齐。
    """
    alias_norm = alias_norm or {}
    by_norm: dict[str, tuple[int, int]] = {}
    for i, g in enumerate(groups, 1):
        by_norm.setdefault(_normalize(g["author"]), (i, len(g["papers"])))

    roster: list[dict] = []
    seen: set[str] = set()
    for name in load_highlight_config().get("featured_authors", []):
        disp = _canon(name, alias_norm)          # 规范显示名
        key = _normalize(disp)
        if not key or key in seen:
            continue
        seen.add(key)
        hit = by_norm.get(key)
        roster.append({
            "name": disp,
            "anchor": hit[0] if hit else None,
            "count": hit[1] if hit else 0,
        })
    return roster


def _safe_author_fn(name: str) -> str:
    """Convert author name to a safe HTML filename, e.g. 'Bryan Kelly' -> 'author_bryan_kelly.html'."""
    return "author_" + _re.sub(r"[^\w]", "_", name.lower()).strip("_") + ".html"


def _papers_by_author(author_name: str, papers: list[dict],
                      alias_norm: dict[str, str] | None = None) -> list[dict]:
    """Find all papers in archive where this author appears (searches authors list directly)."""
    alias_norm = alias_norm or {}
    canonical = _canon(author_name, alias_norm)
    norm_canonical = _normalize(canonical)
    target_norms: set[str] = {norm_canonical}
    for k, v in alias_norm.items():
        if _normalize(v) == norm_canonical:
            target_norms.add(k)
    result = [
        p for p in papers
        if any(_normalize(a) in target_norms for a in (p.get("authors") or []))
    ]
    return sorted(result, key=_date_key, reverse=True)


def render(papers: list[dict], output_dir: Path | None = None) -> Path:
    out_dir = output_dir or _DOCS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    updated_at = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M") + " (北京时间 UTC+8)"
    total = len(papers)

    # 各期刊分桶 + 概要
    by_src: dict[str, list] = defaultdict(list)
    for p in papers:
        by_src[p.get("source") or "?"].append(p)

    sources = sorted((s for s in by_src if s not in _NAV_EXCLUDE), key=_journal_order_index)
    journals = []
    for s in sources:
        ps = by_src[s]
        latest = max((p.get("published_date") or "" for p in ps), default="")
        journals.append({
            "source": s,
            "count": len(ps),
            "href": f"{s}.html",
            "latest": latest or None,
        })

    # 首页"最新收录"= first_seen 最大的那一批（最近一次刷新抓到的）
    all_fs = [p.get("first_seen") for p in papers if p.get("first_seen")]
    latest_fs = max(all_fs) if all_fs else None
    latest_papers = sorted(
        [p for p in papers
         if p.get("first_seen") == latest_fs
         and p.get("source") != "SSRN"]   # SSRN 已有大佬前瞻，不重复出现在首页最新栏
        if latest_fs else [],
        key=_date_key, reverse=True,
    )

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )

    # alias_norm 和 leaderboard 提前算，供 index.html 嵌入作者快速跳转数据
    _aliases = load_highlight_config().get("featured_aliases") or {}
    alias_norm = {_normalize(k): v for k, v in _aliases.items() if k and v}
    leaderboard_rows = active_top_n(papers, n=_LEADERBOARD_N)
    for row in leaderboard_rows:
        row["author_href"] = _safe_author_fn(row["name"])
    author_index = [{"name": r["name"], "href": r["author_href"]} for r in leaderboard_rows]

    # 大佬前瞻 = 只收 SSRN working papers（已发表论文在各期刊页以加粗作者名标注）
    featured_papers = [p for p in papers
                       if p.get("is_featured") and p.get("source") == "SSRN"]
    common = {
        "updated_at": updated_at,
        "total": total,
        "journals": journals,
        "css_href": "style.css",
        "home_href": "index.html",
        "featured": {
            "name": FEATURED_NAME,
            "href": _FEATURED_HREF,
            "count": len(featured_papers),
        },
        "leaderboard_nav": {
            "name": _LEADERBOARD_NAME,
            "href": _LEADERBOARD_HREF,
        },
    }

    # 首页
    index_html = env.get_template("index.html.j2").render(
        active="__home__",
        latest_papers=latest_papers,
        latest_date=latest_fs,
        author_index=author_index,
        **common,
    )
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    # 「大佬前瞻」页（跨期刊，按大佬分组）
    featured_groups = _group_by_author(featured_papers, alias_norm)
    featured_html = env.get_template("featured.html.j2").render(
        active="__featured__",
        papers_total=len(featured_papers),
        groups=featured_groups,
        featured_roster=_featured_roster(featured_groups, alias_norm),
        **common,
    )
    (out_dir / _FEATURED_HREF).write_text(featured_html, encoding="utf-8")

    # 「发文活跃榜」独立页（rows + author_href 已在上方预算好）
    leaderboard_html = env.get_template("leaderboard.html.j2").render(
        active="__leaderboard__",
        rows=leaderboard_rows,
        **common,
    )
    (out_dir / _LEADERBOARD_HREF).write_text(leaderboard_html, encoding="utf-8")

    # 每个活跃榜学者生成独立页面（按来源分组）
    author_tmpl = env.get_template("author.html.j2")
    for row in leaderboard_rows:
        a_papers = _papers_by_author(row["name"], papers, alias_norm)
        if not a_papers:
            continue
        by_src_a: dict[str, list] = defaultdict(list)
        for p in a_papers:
            by_src_a[p.get("source") or "?"].append(p)
        groups_a = [
            {"source": s, "papers": sorted(by_src_a[s], key=_date_key, reverse=True)}
            for s in sorted(by_src_a, key=_journal_order_index)
        ]
        page = author_tmpl.render(
            author=row["name"],
            papers_total=len(a_papers),
            groups=groups_a,
            active="__author__",
            **common,
        )
        (out_dir / row["author_href"]).write_text(page, encoding="utf-8")

    # 每个期刊一页
    jtmpl = env.get_template("journal.html.j2")
    for s in sources:
        groups = _group_by_topic(by_src[s])
        page = jtmpl.render(
            active=s,
            source=s,
            papers_total=len(by_src[s]),
            groups=groups,
            **common,
        )
        (out_dir / f"{s}.html").write_text(page, encoding="utf-8")

    logger.info("已渲染多页站：首页 + 大佬前瞻 + %d 个学者页 + %d 个期刊页（共 %d 篇）-> %s",
                len(leaderboard_rows), len(sources), total, index_path)
    return index_path


if __name__ == "__main__":
    # 自测：用真实归档（不调 API），渲染整站，浏览器打开 docs/index.html 预览。
    import json

    from src.highlight import annotate_all

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    arch = json.loads(
        (_PROJECT_ROOT / "data" / "papers.json").read_text(encoding="utf-8"))
    path = render(annotate_all(arch))
    print("已生成多页站，入口：", path)
