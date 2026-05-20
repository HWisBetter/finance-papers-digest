"""数据源调度：读 config/sources.yaml，按 type 分派到对应 fetcher。

main.py 不再硬编码 SOURCES，改为 load_sources() + fetch_source()。
加期刊只改 yaml；新增一种出版商时，在 _DISPATCH 里加一行即可。
"""

import logging
from pathlib import Path

import yaml

from src.fetchers.cambridge import fetch_cambridge
from src.fetchers.crossref import fetch_crossref
from src.fetchers.nber import fetch_nber
from src.fetchers.wiley import fetch_wiley

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCES_PATH = _PROJECT_ROOT / "config" / "sources.yaml"


def load_sources() -> list[dict]:
    """读 sources.yaml，返回 enabled 的源配置列表。缺失/损坏则返回空并告警。"""
    if not _SOURCES_PATH.exists():
        logger.error("sources.yaml 不存在：%s", _SOURCES_PATH)
        return []
    try:
        with _SOURCES_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as ex:
        logger.error("读取 sources.yaml 失败：%r", ex)
        return []

    all_src = data.get("sources") or []
    enabled = [s for s in all_src if s.get("enabled")]
    disabled = [s.get("name") for s in all_src if not s.get("enabled")]
    if disabled:
        logger.info("已跳过未启用的源：%s", disabled)
    return enabled


def _fetch_ssrn_stub(cfg: dict) -> list[dict]:
    logger.info("SSRN 为留白桩（watch_authors 待提供），本次返回 0 篇")
    return []


def fetch_source(cfg: dict) -> list[dict]:
    """按单条源配置分派抓取。未知 type 记错误并返回空，不影响其他源。"""
    name = cfg.get("name", "?")
    stype = cfg.get("type")

    if stype == "nber_rss":
        return fetch_nber()
    if stype == "wiley_rss":
        return fetch_wiley(str(cfg["issn"]), name)
    if stype == "cambridge_rss":
        return fetch_cambridge(cfg["feed_url"], name)
    if stype == "crossref":
        return fetch_crossref(str(cfg["issn"]), name, int(cfg.get("rows", 40)))
    if stype == "ssrn_stub":
        return _fetch_ssrn_stub(cfg)

    logger.error("源 %s 的 type=%r 未知，跳过", name, stype)
    return []
