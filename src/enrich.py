"""缺摘要补全：先走 Elsevier 官方接口，再用 Semantic Scholar 兜底。

为什么这样分层（2026-05-18 与用户敲定，并都已实测）：
  - JFE 经 Crossref 无摘要（Elsevier 不存），OpenAlex 实测也没有。
  - **Elsevier 官方 Abstract Retrieval API** 是合规的"用你的访问权"正路：
    `GET api.elsevier.com/content/abstract/doi/<DOI>`，带免费注册的 APIKey
    （X-ELS-APIKey）。匿名访问不稳且对新文返回空，所以需要 key；带 key 后
    按 DOI 稳定返回，无爬虫、无封号风险、合同允许。**没配 key 自动跳过这层。**
  - **Semantic Scholar 批量端点** 作兜底：能补回它已收录的真论文，最新文
    它常未收录（返回 null）——那就维持"如实标注"，等以后某次重跑再补
    （配合 main.py 的 abstract_missing 可复活机制，覆盖率随时间增长）。

只对【id 是 DOI】的缺摘要文章生效：JFE/MS/RFS 受益；NBER 的 id 是
wXXXXX 不是 DOI，且本来就有摘要，不走这条。

设计原则（与全项目一致）：纯增强、尽力而为。任何网络/限流/格式/鉴权异常
都只记日志，绝不抛出，绝不因"补摘要失败"丢文章或拖垮流水线（见 never-drop）。
就地把 paper["abstract"] 补上并返回成功条数；是否重做 AI 总结由上层 main.py 决定。
"""

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()  # 与 summarizer 一致：从项目根 .env 读 ELSEVIER_API_KEY 等

logger = logging.getLogger(__name__)

# ---- Elsevier 官方 Article Retrieval API（ScienceDirect，逐 DOI，无批量端点）----
# 实测要点（2026-05-18）：用 *Abstract* Retrieval（基于 Scopus）对最新 JFE
# 的 coredata.dc:description 是空的（Scopus 索引滞后，和 S2 同病）；改用
# *Article* Retrieval（ScienceDirect），最新 JFE 当即返回完整摘要，且只需
# APIKey、无需 insttoken。摘要在 full-text-retrieval-response.coredata.dc:description。
_ELSEVIER_URL = "https://api.elsevier.com/content/article/doi/{doi}?httpAccept=application/json"
_ELS_SLEEP = 0.34          # 礼貌间隔（带 key 时配额宽松，仍温和）
_ELS_TIMEOUT = 30
_ELS_RETRY_WAIT = 2.0      # 偶发 5xx/超时（Elsevier 边缘抖动）时轻重试一次

# ---- Semantic Scholar 批量端点（兜底）----
_S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch?fields=abstract"
_S2_UA = "finance-papers-digest/1.0 (academic personal digest)"
_S2_CHUNK = 100
_S2_SLEEP = 1.2

_ABSTRACT_PREFIX_RE = re.compile(r"^\s*abstract\s*:?\s*", re.IGNORECASE)


_NBER_W_RE = re.compile(r"^w\d+$", re.IGNORECASE)


def _lookup_doi(p: dict) -> str:
    """返回用于补摘要的 DOI。Crossref/Wiley 源 id 本身就是 DOI；
    NBER 回填把 id 归一成了 wXXXXX（为与 RSS 去重一致），这里还原成
    其真实 DOI 10.3386/wXXXXX —— 否则 OpenAlex/S2 查不到，NBER 全军覆没。
    取不到可用 DOI 返回 ""（该篇不进补全）。"""
    pid = (p.get("id") or "").strip()
    if pid.lower().startswith("10."):
        return pid
    if p.get("source") == "NBER" and _NBER_W_RE.match(pid):
        return "10.3386/" + pid.lower()
    return ""


def _clean(text: str) -> str:
    if not text:
        return ""
    text = _ABSTRACT_PREFIX_RE.sub("", text)
    return " ".join(text.split()).strip()


# ---------------- Elsevier 层 ----------------

def _elsevier_headers() -> dict | None:
    """没配 ELSEVIER_API_KEY 就返回 None（上层据此跳过整层）。"""
    key = (os.getenv("ELSEVIER_API_KEY") or "").strip()
    if not key:
        return None
    headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
    token = (os.getenv("ELSEVIER_INST_TOKEN") or "").strip()
    if token:
        headers["X-ELS-Insttoken"] = token
    return headers


def _els_get(url: str, headers: dict) -> tuple[dict | None, str]:
    """单次请求；返回 (json或None, status)。status 见 _elsevier_abstract。"""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_ELS_TIMEOUT) as r:
            return json.load(r), "ok"
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403):
            return None, "auth"
        if ex.code == 404:
            return None, "empty"
        return None, "err"          # 4xx/5xx 其它（含 429/5xx 抖动）
    except Exception:
        return None, "err"          # 超时/连接/JSON 异常


def _elsevier_abstract(doi: str, headers: dict) -> tuple[str, str]:
    """取单篇摘要。返回 (abstract, status)。

    status: 'ok' 拿到 | 'empty' 有响应但无摘要（未收录/不在权限内）
            | 'auth' 鉴权失败（key 无效或需 insttoken）| 'err' 网络/格式问题
    对 'err'（Elsevier 边缘偶发 5xx/超时）轻重试一次再判定。
    """
    url = _ELSEVIER_URL.format(doi=urllib.parse.quote(doi))
    data, st = _els_get(url, headers)
    if st == "err":
        time.sleep(_ELS_RETRY_WAIT)
        data, st = _els_get(url, headers)
    if st != "ok" or data is None:
        return "", st

    core = (data.get("full-text-retrieval-response", {}) or {}).get("coredata", {}) or {}
    desc = core.get("dc:description")
    if isinstance(desc, dict):  # 偶见嵌套结构，尽力取文本
        desc = json.dumps(desc, ensure_ascii=False)
    abstract = _clean(desc or "")
    return (abstract, "ok") if abstract else ("", "empty")


def _enrich_via_elsevier(targets: list[dict]) -> int:
    headers = _elsevier_headers()
    if headers is None:
        logger.info("未配 ELSEVIER_API_KEY，跳过 Elsevier 官方接口（用 S2 兜底）")
        return 0

    logger.info("Elsevier 官方接口：尝试补 %d 篇缺摘要", len(targets))
    filled = auth_err = other_err = 0
    for i, p in enumerate(targets):
        doi = _lookup_doi(p)
        if doi.startswith("10.3386"):  # NBER：Elsevier 绝无，跳过省时
            continue
        abstract, status = _elsevier_abstract(doi, headers)
        if status == "ok":
            p["abstract"] = abstract
            filled += 1
        elif status == "auth":
            auth_err += 1
        elif status == "err":
            other_err += 1
        if i + 1 < len(targets):
            time.sleep(_ELS_SLEEP)

    logger.info("Elsevier 补回 %d / %d 篇（空/未授权 %d，网络异常 %d）",
                filled, len(targets), len(targets) - filled - other_err, other_err)
    if auth_err:
        logger.warning(
            "Elsevier 有 %d 篇返回鉴权失败：请检查 .env 里 ELSEVIER_API_KEY 是否正确；"
            "若 key 没问题，多半是这些订阅内容需要机构令牌——"
            "向单位图书馆申请 Elsevier Institutional Token，填到 .env 的 "
            "ELSEVIER_INST_TOKEN 即可。", auth_err)
    return filled


# ---------------- OpenAlex 层（Wiley/Cambridge/Oxford 等覆盖好）----------------
# 实测：OpenAlex 对 Elsevier(JFE) 没有摘要，但对 JFQA(Cambridge)/JF/JAR(Wiley)/
# RFS(Oxford) 多有，以 abstract_inverted_index（词->位置）形式给出，需重建。
_OPENALEX_URL = "https://api.openalex.org/works/https://doi.org/{doi}"
# OpenAlex 礼貌池：UA 带 mailto 即可，免 key
_OA_UA = "finance-papers-digest/1.0 (mailto:digest@example.com)"
_OA_SLEEP = 0.12


def _reconstruct(inv: dict) -> str:
    """abstract_inverted_index {word:[pos,...]} -> 正常顺序文本。"""
    pos: dict[int, str] = {}
    for word, idxs in (inv or {}).items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[k] for k in sorted(pos))


def _enrich_via_openalex(targets: list[dict]) -> int:
    logger.info("OpenAlex：尝试补 %d 篇缺摘要", len(targets))
    filled = 0
    for i, p in enumerate(targets):
        url = _OPENALEX_URL.format(doi=urllib.parse.quote(_lookup_doi(p)))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _OA_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                inv = json.load(r).get("abstract_inverted_index")
        except Exception:
            inv = None
        if inv:
            text = _clean(_reconstruct(inv))
            if text:
                p["abstract"] = text
                filled += 1
        if i + 1 < len(targets):
            time.sleep(_OA_SLEEP)
    logger.info("OpenAlex 补回 %d / %d 篇", filled, len(targets))
    return filled


# ---------------- Semantic Scholar 兜底层 ----------------

def _s2_post(dois: list[str]) -> list[dict | None]:
    body = json.dumps({"ids": [f"DOI:{d}" for d in dois]}).encode("utf-8")
    req = urllib.request.Request(
        _S2_BATCH, data=body,
        headers={"Content-Type": "application/json", "User-Agent": _S2_UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
        return data if isinstance(data, list) else []
    except Exception as ex:  # 含 429 限流、超时、JSON 异常
        logger.warning("Semantic Scholar 批量请求失败（%r），本批跳过、下次再试", ex)
        return []


def _enrich_via_s2(targets: list[dict]) -> int:
    by_doi: dict[str, dict] = {}
    for p in targets:
        by_doi.setdefault(_lookup_doi(p), p)
    dois = list(by_doi)
    logger.info("Semantic Scholar 兜底：尝试补 %d 篇", len(dois))

    filled = 0
    for i in range(0, len(dois), _S2_CHUNK):
        chunk = dois[i:i + _S2_CHUNK]
        for doi, res in zip(chunk, _s2_post(chunk)):
            ab = (res or {}).get("abstract") if isinstance(res, dict) else None
            if ab and ab.strip():
                by_doi[doi]["abstract"] = _clean(ab)
                filled += 1
        if i + _S2_CHUNK < len(dois):
            time.sleep(_S2_SLEEP)

    logger.info("Semantic Scholar 补回 %d / %d 篇（其余 S2 尚未收录，下次再试）",
                filled, len(dois))
    return filled


# ---------------- 对外入口 ----------------

def enrich_missing_abstracts(papers: list[dict]) -> int:
    """对【缺摘要且 id 是 DOI】的论文就地补 abstract：先 Elsevier，再 S2 兜底。

    返回成功补上的总篇数。papers 会被就地修改。
    """
    targets = [
        p for p in papers
        if not (p.get("abstract") or "").strip() and _lookup_doi(p)
    ]
    if not targets:
        return 0
    logger.info("缺摘要待补全 %d 篇（Elsevier → OpenAlex → Semantic Scholar）",
                len(targets))

    filled = _enrich_via_elsevier(targets)  # JFE(Elsevier) 主力

    still = [p for p in targets if not (p.get("abstract") or "").strip()]
    if still:
        filled += _enrich_via_openalex(still)  # JFQA/JF/JAR/RFS 等主力

    still = [p for p in targets if not (p.get("abstract") or "").strip()]
    if still:
        filled += _enrich_via_s2(still)        # 最后兜底

    logger.info("补全合计 %d / %d 篇（其余维持 abstract_missing，下次重跑再试）",
                filled, len(targets))
    return filled


if __name__ == "__main__":
    # 自测：拿当前归档里缺摘要的 DOI 文章跑一遍，看能补回几篇。
    from pathlib import Path

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    arch = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "papers.json")
        .read_text(encoding="utf-8"))
    missing = [p for p in arch if not (p.get("abstract") or "").strip()]
    print(f"归档缺摘要 {len(missing)} 篇，尝试补全……")
    n = enrich_missing_abstracts(missing)
    print(f"补回 {n} 篇")
    for p in missing:
        if (p.get("abstract") or "").strip():
            print(" OK", p["id"], "|", p["title"][:55])
