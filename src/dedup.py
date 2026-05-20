"""论文去重：给每篇论文算一个稳定指纹，并记录"见过哪些"。

为什么需要：fetcher 每天抓到的 RSS 里有大量是昨天就出现过的旧论文。
我们只想对【新】论文调 AI 总结（省钱、省时）。做法是给每篇论文算一个
基于内容的指纹（hash），把见过的指纹存进 data/seen.json，下次比对即可。

接口：
  hash_paper(paper) -> str           一篇论文的 SHA256 指纹
  load_seen()        -> set[str]     读出所有见过的指纹（文件没有就空集合）
  save_seen(hashes)  -> None         把指纹集合写回 data/seen.json

指纹规则（已和你确认）：对 `标题 + 第一作者` 先做归一化
（转小写、去首尾空白、合并内部连续空白），再 SHA256。
这样 "Asset Pricing" 和 " asset  pricing " 会被认成同一篇，
避免因为空格/大小写差异把同一篇当成新论文重复总结。
"""

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 用 __file__ 推算项目根：src/dedup.py -> parents[1] 就是项目根目录。
# 这样不管从哪个目录运行脚本，路径都对（不依赖当前工作目录）。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEEN_PATH = _PROJECT_ROOT / "data" / "seen.json"

# 拼接标题和作者时用的分隔符：用控制字符（单元分隔符 0x1F），
# 正常论文标题/人名不会包含它，避免 "AB"+"C" 和 "A"+"BC" 撞车。
_SEP = "\x1f"


def _normalize(text: str) -> str:
    """归一化：转小写 + 去首尾空白 + 把内部连续空白压成单个空格。

    str.split() 不带参数时会按任意空白切分并自动丢弃空串，
    再用单空格 join，等于一次性完成"去首尾 + 合并内部空白"。
    """
    if not text:
        return ""
    return " ".join(text.split()).lower()


def hash_paper(paper: dict) -> str:
    """返回这篇论文的 SHA256 指纹（十六进制字符串）。

    用 标题 + 第一作者 计算。NBER 等没有作者时，第一作者用空串，
    此时退化为"按标题去重"，对工作论文场景足够。
    """
    title = _normalize(paper.get("title", ""))
    authors = paper.get("authors") or []
    first_author = _normalize(authors[0]) if authors else ""

    fingerprint_src = f"{title}{_SEP}{first_author}"
    return hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()


def load_seen() -> set[str]:
    """读出 data/seen.json 里所有见过的指纹，返回 set[str]。

    文件不存在（首次运行）、为空、或内容损坏，都返回空集合而不报错——
    最坏结果只是把旧论文当新的重新总结一次，不会让整个流程崩。
    """
    if not SEEN_PATH.exists():
        logger.info("seen.json 不存在（首次运行？），按空集合处理：%s", SEEN_PATH)
        return set()
    try:
        with SEEN_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("seen.json 内容不是列表，忽略并按空集合处理")
            return set()
        return set(data)
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning("读取 seen.json 失败（%r），按空集合处理", ex)
        return set()


def save_seen(hashes: set[str]) -> None:
    """把指纹集合写回 data/seen.json。

    存成【排序后的列表】：JSON 不支持集合，存列表；排序后每次输出稳定，
    将来把 data/ 提交到 git 给 GitHub Actions 用时，diff 干净好看。
    """
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 存在
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(hashes), f, ensure_ascii=False, indent=2)
    logger.info("已写入 seen.json，共 %d 个指纹：%s", len(hashes), SEEN_PATH)


if __name__ == "__main__":
    # 自测：验证(1)指纹稳定 (2)归一化生效 (3)save/load 往返。
    # 注意：往返测试写到临时文件，绝不碰真实的 data/seen.json。
    import tempfile

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )

    p1 = {"title": "Asset Pricing", "authors": ["Jane Doe", "John Roe"]}
    p2 = {"title": "  asset   PRICING ", "authors": ["  jane DOE "]}  # 应与 p1 同指纹
    p3 = {"title": "Asset Pricing", "authors": ["Someone Else"]}      # 不同作者 -> 不同
    p_nber = {"title": "No Authors Paper", "authors": []}             # 无作者

    h1, h2, h3 = hash_paper(p1), hash_paper(p2), hash_paper(p3)
    print("p1 指纹:", h1)
    print("p2 指纹:", h2, "  与 p1 相同?", h1 == h2, "（应为 True：归一化生效）")
    print("p3 指纹:", h3, "  与 p1 不同?", h1 != h3, "（应为 True：作者不同）")
    print("无作者论文指纹:", hash_paper(p_nber), "（不报错即可）")

    real_seen = load_seen()
    print(f"\n当前真实 seen.json 指纹数: {len(real_seen)}")

    # 往返测试用临时文件
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sample = {h1, h3}
        original = globals()["SEEN_PATH"]
        globals()["SEEN_PATH"] = tmp_path  # 临时改指向，避免动真实文件
        save_seen(sample)
        loaded = load_seen()
        globals()["SEEN_PATH"] = original
        print("save/load 往返一致?", loaded == sample, "（应为 True）")
    finally:
        tmp_path.unlink(missing_ok=True)
