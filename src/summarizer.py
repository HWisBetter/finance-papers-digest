"""调用 DeepSeek API，给一篇论文生成中文总结、关键词、一句话点评。

接口：summarize_paper(paper: dict) -> dict
  输入：fetcher 产出的 paper dict（含 title/abstract/source 等）
  输出：在原 dict 基础上新增 3 个字段的【新】dict（不改原 dict）：
    - summary:    str        一段话总结：基于摘要、忠实完整地讲清这篇
                             论文在做什么；完整性优先于简洁；中文为主，
                             关键学术术语保留英文原文
    - keywords:   list[str]  3-5 个关键词（专业术语可保留英文）
    - ai_comment: str        一两句中文点评，点出贡献/亮点

关键设计（都是按真实探测结果定的，不是拍脑袋）：
  1. deepseek-v4-flash 是"推理模型"：回答前会消耗一批 reasoning token，
     这些 token 也算进 max_tokens。所以 max_tokens 必须给足（这里 1200），
     否则思考没结束就被截断，content 会是空字符串。
  2. 最终答案在 message.content，是干净 JSON；配合
     response_format={"type":"json_object"} 强制 JSON，最稳。
  3. 用 tenacity 做指数退避重试（最多 3 次）：网络抖动、限流、偶发
     非法 JSON 都会触发重试；3 次仍失败就抛异常，交给 main.py 决定
     （单篇失败不拖垮整体——容错在 main.py 那层做）。
  4. 模型名 / base_url 走环境变量，默认值写好，方便以后不改代码就能
     切到 deepseek-v4-pro 或换地址。
"""

import json
import logging
import os

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

load_dotenv()  # 从项目根目录 .env 读取 DEEPSEEK_API_KEY

# 这些都可被环境变量覆盖；默认值是已验证可用的配置
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# 推理模型，回答前会消耗 reasoning token；现在 summary 是"完整一段话"
# 而非一句话，输出更长，额度要更足（reasoning + 较长中文段落）
_MAX_TOKENS = 2000

# 期刊页"主题分组"用的固定主题表（标准金融 12 类 + 其他兜底）。
# 模型只能从这里选【唯一一个】，保证全站分组一致、不会发散成上百种主题。
TOPICS = (
    "资产定价",
    "公司金融",
    "银行与金融中介",
    "市场微观结构",
    "行为金融",
    "金融计量/机器学习",
    "宏观金融",
    "国际金融",
    "家庭金融",
    "公司治理",
    "会计与信息披露",
    "衍生品",
)
_OTHER_TOPIC = "其他"
_TOPIC_SET = set(TOPICS) | {_OTHER_TOPIC}
_TOPIC_LIST_STR = "、".join(TOPICS)

_SYSTEM_PROMPT = (
    "你是金融与经济学术论文阅读助手。用户会给你一篇论文的标题和【摘要】。"
    "注意：你只能看到摘要、看不到全文，绝不要编造摘要里没有的数据、方法或结论。\n\n"
    "请基于摘要，用简体中文输出严格的 JSON，只包含这四个键："
    "summary、keywords、ai_comment、topic。不要 markdown 代码块、不要任何额外文字。\n\n"
    "各字段要求：\n"
    "- summary：用一段话（可多句、连贯成一段）忠实、完整地讲清这篇论文在做"
    "什么。尽量覆盖：研究问题/动机、采用的方法或机制、研究对象/数据/情境、"
    "主要发现与结论。【完整性优先于简洁】——只要摘要里有的关键信息和结论都"
    "要保留，不要为了短而牺牲内容；同时行文要通顺、便于阅读。专业且关键的"
    "学术术语（模型名、方法名、核心概念、英文缩写如 LLM 等）保留英文原文，"
    "其余用中文，使中文流畅的同时保持术语准确与原始表达的完整。不要复述论文"
    "标题或作者（页面已单独展示），直接讲内容。\n"
    "- keywords：3 到 5 个关键词的列表，定位论文主题。【语言规则要严格一致】："
    "以中文为主；只有【公认的英文专有术语/缩写】才保留英文原文（如 CAPM、"
    "GARCH、LLM、NLP、ESG、IPO、M&A、DSGE、VAR、ETF、SVB 等），其余一律用"
    "规范中文（如 资产定价、动量效应、流动性、公司治理）。不要中英混写同一个"
    "词、不要给括号翻译、不要整列都用英文。\n"
    "- ai_comment：一到两句中文点评，点出这篇论文的贡献、亮点或值得注意之处，"
    "不要简单重复 summary。\n"
    "- topic：从下面固定列表里选【唯一一个】最贴切的主题，必须【原样照抄】"
    "列表中的中文，不要自创、不要加书名号：\n  "
    + _TOPIC_LIST_STR + "\n"
    "  只有当以上没有一个贴切时，才填『" + _OTHER_TOPIC + "』。"
)


class SummarizeError(Exception):
    """总结失败（重试若干次后仍无法得到合法结果）。"""


def _build_user_prompt(paper: dict) -> str:
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    source = paper.get("source", "")
    return (
        f"来源：{source}\n"
        f"标题：{title}\n"
        f"摘要：{abstract}\n\n"
        "请按系统提示的 JSON 格式输出。"
    )


def _validate(data: dict) -> dict:
    """校验模型返回的 JSON 结构合法，否则抛异常触发重试。"""
    if not isinstance(data, dict):
        raise SummarizeError(f"返回不是 JSON 对象：{type(data)}")

    summary = data.get("summary")
    keywords = data.get("keywords")
    ai_comment = data.get("ai_comment")

    if not isinstance(summary, str) or not summary.strip():
        raise SummarizeError(f"summary 非法：{summary!r}")
    if not isinstance(keywords, list) or not (
        1 <= len(keywords) <= 8
    ) or not all(isinstance(k, str) and k.strip() for k in keywords):
        raise SummarizeError(f"keywords 非法：{keywords!r}")
    if not isinstance(ai_comment, str) or not ai_comment.strip():
        raise SummarizeError(f"ai_comment 非法：{ai_comment!r}")

    # topic 是次要字段：不合法不重试/不丢掉好总结，直接归类到"其他"。
    topic = data.get("topic")
    topic = topic.strip() if isinstance(topic, str) else ""
    if topic not in _TOPIC_SET:
        topic = _OTHER_TOPIC

    return {
        "summary": summary.strip(),
        "keywords": [k.strip() for k in keywords],
        "ai_comment": ai_comment.strip(),
        "topic": topic,
    }


# OpenAI 客户端复用一个实例即可（线程安全、内部连接池）
_client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=DEEPSEEK_BASE_URL)


@retry(
    # 网络/限流/超时/非法 JSON 都重试；指数退避；最多 3 次；3 次后抛原异常
    retry=retry_if_exception_type(
        (
            APIError,
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            SummarizeError,
            json.JSONDecodeError,
        )
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _call_deepseek(paper: dict) -> dict:
    """真正调用 API 并解析校验，失败抛异常（被 tenacity 捕获重试）。"""
    resp = _client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(paper)},
        ],
        response_format={"type": "json_object"},
        max_tokens=_MAX_TOKENS,
        temperature=0.3,
    )
    content = resp.choices[0].message.content or ""
    if not content.strip():
        # 通常是 max_tokens 不够导致被截断；抛出触发重试
        raise SummarizeError(
            f"模型返回空 content（finish_reason="
            f"{resp.choices[0].finish_reason}）"
        )
    data = json.loads(content)  # 非法 JSON 抛 JSONDecodeError -> 重试
    return _validate(data)


_CLASSIFY_SYS = (
    "你是金融论文分类助手。根据论文标题和摘要，从下面固定列表里选"
    "【唯一一个】最贴切的主题，输出严格 JSON：{\"topic\": \"<列表中的一项>\"}。"
    "必须原样照抄列表中的中文，不要解释、不要 markdown。\n"
    + _TOPIC_LIST_STR + "\n只有都不贴切才用『" + _OTHER_TOPIC + "』。"
)


@retry(
    retry=retry_if_exception_type(
        (APIError, APIConnectionError, APITimeoutError, RateLimitError,
         SummarizeError, json.JSONDecodeError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def classify_topic(title: str, abstract: str) -> str:
    """只做主题分类（给存量论文回填 topic 用，避免重做整段总结）。

    返回固定表中的一个主题；解析异常归"其他"；API 硬失败抛 SummarizeError
    （调用方按篇 try/except，失败的下次再补，不影响其他篇）。
    """
    user = f"标题：{title}\n摘要：{abstract}\n\n请按要求只输出 {{\"topic\": ...}}。"
    resp = _client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYS},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,          # 输出极短，但推理模型仍需足够 reasoning 额度
        temperature=0,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise SummarizeError("分类返回空 content")
    topic = json.loads(content).get("topic")
    topic = topic.strip() if isinstance(topic, str) else ""
    return topic if topic in _TOPIC_SET else _OTHER_TOPIC


_KEYWORDS_SYS = (
    "你是金融论文关键词助手。根据论文标题和摘要，给出 3 到 5 个定位主题的"
    "关键词，输出严格 JSON：{\"keywords\": [\"...\", ...]}。\n"
    "【语言规则严格一致】：以中文为主；只有公认的英文专有术语/缩写才保留"
    "英文原文（如 CAPM、GARCH、LLM、NLP、ESG、IPO、M&A、DSGE、VAR、ETF），"
    "其余一律规范中文（如 资产定价、动量效应、流动性、公司治理）。"
    "不要中英混写同一个词、不要给括号翻译、不要整列都用英文、不要解释。"
)


@retry(
    retry=retry_if_exception_type(
        (APIError, APIConnectionError, APITimeoutError, RateLimitError,
         SummarizeError, json.JSONDecodeError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def extract_keywords(title: str, abstract: str) -> list[str]:
    """只重抽关键词（给存量论文统一关键词语言用，避免重做整段总结）。

    返回 3-5 个关键词的列表（按既定中文为主规则）；非法/空抛
    SummarizeError 由 tenacity 重试，3 次仍失败再抛给调用方按篇兜底。
    """
    user = f"标题：{title}\n摘要：{abstract}\n\n请按要求只输出 {{\"keywords\": [...]}}。"
    resp = _client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": _KEYWORDS_SYS},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise SummarizeError("关键词返回空 content")
    kws = json.loads(content).get("keywords")
    if not isinstance(kws, list):
        raise SummarizeError(f"keywords 非法：{kws!r}")
    out = [k.strip() for k in kws if isinstance(k, str) and k.strip()]
    if not (1 <= len(out) <= 8):
        raise SummarizeError(f"keywords 数量非法：{out!r}")
    return out[:5]


def summarize_paper(paper: dict) -> dict:
    """给一篇 paper 生成 summary/keywords/ai_comment，返回新增字段后的新 dict。

    重试 3 次仍失败会抛 SummarizeError —— 由调用方（main.py）捕获，
    保证单篇失败不影响其他论文。
    """
    title = paper.get("title", "<无标题>")

    # 缺摘要的正式文章：不调 API（无内容可总结，硬调只会让模型瞎编），
    # 如实标注 abstract_missing 并给出诚实占位。绝不丢弃文章——
    # 渲染时仍展示标题/作者/原文链接，用户可自行点开阅读。
    if not (paper.get("abstract") or "").strip():
        logger.warning("无摘要，跳过 AI 总结（保留条目）：[%s] %s",
                        paper.get("source"), title[:60])
        return {
            **paper,
            "abstract_missing": True,
            # 注意：这段文案【不要】出现独立的 "AI" 字样——highlight 的整词
            # 正则 \b(ai|...)\b 会把它误判成 AI 相关文章（曾导致高亮虚高）。
            "summary": "⚠️ 未能获取本文摘要（可能尚未发布或抓取失败），"
                       "暂无自动摘要总结，请点击下方原文链接阅读。",
            "keywords": [],
            "ai_comment": "",
            "topic": _OTHER_TOPIC,  # 无内容可分类
        }

    logger.info("开始总结：[%s] %s", paper.get("source"), title[:60])
    try:
        result = _call_deepseek(paper)
    except Exception as ex:
        logger.error("总结失败（已重试）：%s | %r", title[:60], ex)
        raise SummarizeError(str(ex)) from ex

    logger.info("总结完成：%s", title[:60])
    # abstract_missing=False 让归档/模板对所有论文有一致字段可判断
    return {**paper, "abstract_missing": False, **result}


if __name__ == "__main__":
    # 自测：各抓 1 篇 NBER + JF 真实论文做真实总结（共 2 次 API 调用，控成本），
    # 用来检验新 prompt 下的总结质量。
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )
    from src.fetchers.jf import fetch_jf
    from src.fetchers.nber import fetch_nber

    samples = []
    nber = fetch_nber()
    if nber:
        samples.append(nber[0])
    jf = fetch_jf()
    if jf:
        samples.append(jf[0])

    if not samples:
        print("没抓到论文，无法自测")
    for paper in samples:
        enriched = summarize_paper(paper)
        print("\n" + "=" * 72)
        print(f"[{enriched['source']}] {enriched['title']}")
        print("-" * 72)
        print("【summary】\n" + enriched["summary"])
        print("\n【keywords】", enriched["keywords"])
        print("【ai_comment】", enriched["ai_comment"])
