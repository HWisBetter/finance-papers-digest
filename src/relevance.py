"""MS / NBER 历史回填的"金融 OR AI 相关"预筛（在花 LLM 总结【之前】过滤）。

为什么要它：MS(~1954) 和 NBER(~4520) 绝大多数是非金融（运营/营销/劳动/
宏观/健康/贸易…）。用户决定 MS/NBER 只收金融或 AI 相关，金融5(JF/JFE/RFS/
JFQA/JAR)则不过滤、全收。本模块只做【便宜的关键词/学科判定】，零 API 成本。

【2026-05-19 收紧版，用户选"强金融信号"】关键设计：
  - 只保留**强金融词**（无歧义）。刻意【不要】security/option/investment/
    trading/valuation 这类裸词——它们在 MS 的运营/信息系统/营销语境里大量
    误命中（real option、capacity investment、cyber security、trading off…）。
    需要更强的限定形式（insider trading、option pricing、equity valuation…）。
  - subject 白名单**只留 finance / accounting**，【移除 generic economics】：
    NBER 全部论文 Crossref subject 基本都是 "Economics"，留着等于不筛。
  - 仍复用 highlight.yaml 的 AI 词表（不重复维护）。
判定：标题或摘要命中强金融词，或命中 AI 词，或 subject 含 finance/accounting
即算相关；否则丢弃（这是用户主动缩小 MS/NBER 范围的决定，金融5 不受影响）。
"""

import logging
import re

from src.highlight import _build_acronym_re, _normalize, load_highlight_config

logger = logging.getLogger(__name__)

# 强金融词（小写）。多为词组或无歧义词；裸词只留确属金融的（dividend、
# mortgage、bond yield 等）。用 \b 词边界匹配。
_FINANCE_TERMS = [
    # 资产定价 / 投资组合
    "asset pricing", "stock return", "equity return", "expected return",
    "risk premium", "cross-section of return", "factor model",
    "asset price", "portfolio choice", "portfolio selection",
    "mutual fund", "hedge fund", "exchange-traded fund", "index fund",
    "sharpe ratio", "capm", "arbitrage pricing", "momentum strateg",
    "value premium", "equity premium", "stochastic discount factor",
    "return predictability", "asset allocation", "asset management",
    # 固定收益 / 衍生品（限定形式，避开裸 option）
    "bond yield", "bond return", "term structure of interest",
    "credit spread", "credit risk", "default risk", "sovereign debt",
    "corporate bond", "government bond", "treasury bond", "yield curve",
    "option pricing", "stock option", "call option", "put option",
    "options market", "implied volatility", "derivative pricing",
    "futures market", "interest rate swap", "credit default swap",
    "fixed income", "interest rate risk",
    # 公司金融 / 治理
    "corporate finance", "capital structure", "financial leverage",
    "dividend polic", "dividend payout", "share repurchase",
    "initial public offering", " ipo", "seasoned equity offering",
    "equity issuance", "mergers and acquisition", "takeover",
    "private equity", "venture capital", "corporate governance",
    "board of directors", "executive compensation", "shareholder value",
    "agency cost", "corporate investment", "capital expenditure",
    "cost of capital", "firm value", "cash holding",
    # 银行 / 中介 / 货币
    "bank lending", "bank loan", "bank credit", "loan syndicat",
    "deposit insurance", "credit supply", "monetary policy",
    "central bank", "financial intermediation", "shadow bank",
    "fintech", "systemic risk", "financial crisis", "bank run",
    "financial stability", "liquidity risk", "funding liquidity",
    "interbank", "capital requirement", "basel",
    # 市场 / 微观结构 / 行为
    "market microstructure", "bid-ask spread", "order flow",
    "high-frequency trading", "market maker", "short selling",
    "short sale", "insider trading", "informed trading",
    "trading volume", "market liquidity", "price discovery",
    "behavioral finance", "investor sentiment", "limits to arbitrage",
    "disposition effect", "retail investor", "institutional investor",
    "mispricing", "market efficiency",
    # 家庭 / 房产 / 保险 / 养老
    "household finance", "consumer finance", "mortgage", "housing market",
    "house price", "pension fund", "retirement saving",
    "financial literacy", "life insurance", "annuit", "student loan",
    "credit card debt", "household debt", "wealth inequality",
    # 会计 / 信息披露
    "earnings management", "earnings announcement", "financial reporting",
    "voluntary disclosure", "accrual", "audit quality", "auditor",
    "analyst forecast", "financial statement", "restatement",
    "accounting quality", "earnings quality",
    # 国际 / 宏观金融
    "exchange rate", "foreign exchange", "currency risk", "capital flow",
    "international finance", "carry trade", "cross-border",
    # 强信号词（比 financ 更精确）
    "financial market", "financial asset", "financial institution",
    "financial system", "financial sector", "financial contract",
    "financial econom",  # financial economics
    "stock market", "securitization", "asset-backed securit",
    "stock price", "equity market",
    "safe asset", "safe securit",   # macrofinance / flight-to-quality
    "portfolio",                     # 金融语境常见；操作/营销不常用此词
]

# subject 只认这两个；【故意不含 generic economics】（NBER 几乎全是 economics）
_RELEVANT_SUBJECTS = ("finance", "accounting")

_FINANCE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _FINANCE_TERMS) + r")",
    re.IGNORECASE,
)

# 复用 highlight 的 AI 词表/缩写，避免两处维护
_cfg = load_highlight_config()
_AI_KEYWORDS = [_normalize(k) for k in _cfg.get("ai_keywords", []) if k]
_AI_ACRO_RE = _build_acronym_re(_cfg.get("ai_acronyms", []))


def is_finance_or_ai_relevant(
    title: str, abstract: str, subjects: list[str] | None = None
) -> bool:
    """MS/NBER 预筛：强金融信号 OR AI 相关返回 True。零 API、纯文本判定。"""
    text = f"{title or ''} {abstract or ''}"
    if _FINANCE_RE.search(text):
        return True

    norm = _normalize(text)
    if any(k in norm for k in _AI_KEYWORDS):
        return True
    if _AI_ACRO_RE and _AI_ACRO_RE.search(norm):
        return True

    for s in subjects or []:
        sl = (s or "").lower()
        if any(rs in sl for rs in _RELEVANT_SUBJECTS):
            return True
    return False
