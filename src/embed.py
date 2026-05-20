"""离线文本嵌入器（语义搜索用）。

关键约束：**必须和浏览器端 transformers.js 用完全相同的模型**，否则
论文向量与查询向量不可比、搜索失效。这里用 Hugging Face 上
`Xenova/multilingual-e5-small` 的【量化 ONNX】（正是 transformers.js
默认加载的那个文件），用 onnxruntime 在本地 CPU 跑——轻依赖、与浏览器
逐位对齐。多语种：中文 query 能匹配英文摘要 / 中文总结。

e5 模型用法约定（务必遵守，否则质量差）：
  - 文档侧文本前缀 "passage: "，查询侧前缀 "query: "
  - 句向量 = last_hidden_state 按 attention_mask 做 mean pooling
  - 再做 L2 归一化（之后点积即余弦相似度）

模型文件缓存在 models/（已 gitignore），首次自动下载 ~30-40MB。
"""

import logging
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

_REPO = "Xenova/multilingual-e5-small"
_HF = "https://huggingface.co/{repo}/resolve/main/{f}"
_MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "multilingual-e5-small"
_FILES = {
    "tokenizer.json": "tokenizer.json",
    "model_quantized.onnx": "onnx/model_quantized.onnx",
}
_MAX_LEN = 512
EMB_DIM = 384

_UA = "Mozilla/5.0 (finance-papers-digest embed downloader)"

_tok = None
_sess = None
_in_names = None


def _download(local: Path, remote_path: str) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    url = _HF.format(repo=_REPO, f=remote_path)
    logger.info("下载模型文件：%s -> %s", url, local)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(local, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _ensure_model() -> None:
    global _tok, _sess, _in_names
    if _sess is not None:
        return
    tok_path = _MODEL_DIR / "tokenizer.json"
    onnx_path = _MODEL_DIR / "model_quantized.onnx"
    if not tok_path.exists():
        _download(tok_path, _FILES["tokenizer.json"])
    if not onnx_path.exists():
        _download(onnx_path, _FILES["model_quantized.onnx"])

    _tok = Tokenizer.from_file(str(tok_path))
    _tok.enable_truncation(max_length=_MAX_LEN)
    _tok.enable_padding()  # 按批内最长 padding
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    _sess = ort.InferenceSession(str(onnx_path),
                                 sess_options=so,
                                 providers=["CPUExecutionProvider"])
    _in_names = {i.name for i in _sess.get_inputs()}
    logger.info("嵌入模型就绪（inputs=%s, dim=%d）", sorted(_in_names), EMB_DIM)


def _embed_batch(texts: list[str]) -> np.ndarray:
    encs = _tok.encode_batch(texts)
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in _in_names:  # XLM-R 不用，但有的导出仍要求
        feed["token_type_ids"] = np.zeros_like(ids)
    feed = {k: v for k, v in feed.items() if k in _in_names}
    last_hidden = _sess.run(None, feed)[0]          # (B, T, 384)
    m = mask[:, :, None].astype(np.float32)
    summed = (last_hidden * m).sum(axis=1)
    counts = np.clip(m.sum(axis=1), 1e-9, None)
    vec = summed / counts                            # mean pooling
    norm = np.linalg.norm(vec, axis=1, keepdims=True)
    return (vec / np.clip(norm, 1e-12, None)).astype(np.float32)  # L2 归一化


def embed(texts: list[str], prefix: str, batch_size: int = 32) -> np.ndarray:
    """给一批文本算归一化句向量。prefix 取 'passage: '（文档）或 'query: '。"""
    _ensure_model()
    out = []
    for i in range(0, len(texts), batch_size):
        chunk = [prefix + (t or "").strip() for t in texts[i:i + batch_size]]
        out.append(_embed_batch(chunk))
    return np.vstack(out) if out else np.zeros((0, EMB_DIM), np.float32)


def embed_passages(texts: list[str], batch_size: int = 32) -> np.ndarray:
    return embed(texts, "passage: ", batch_size)


def embed_query(text: str) -> np.ndarray:
    return embed([text], "query: ")[0]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    q = embed_query("货币政策对银行信贷的影响")
    docs = embed_passages([
        "Monetary policy transmission through bank lending and credit supply",
        "A theory of optimal corporate dividend payout under taxation",
        "The psychology of macroeconomic expectations and animal spirits",
    ])
    sims = docs @ q
    print("query: 货币政策对银行信贷的影响")
    for s, d in sorted(zip(sims, [
        "[1] Monetary policy ... bank lending",
        "[2] dividend payout under taxation",
        "[3] psychology of macro expectations",
    ]), reverse=True):
        print(f"  cos={s:.3f}  {d}")
    print("（[1] 应明显最高，说明跨中英语义检索正常）")
