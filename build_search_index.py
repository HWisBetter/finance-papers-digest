"""给全库论文算 embedding 并生成浏览器用的语义搜索索引。

产物：
  data/embeddings.json   {hash: [int8 x384]}  —— 持久化、按 hash 增量
                          （只对新论文算，日常便宜；可断点续传）
  docs/search-index.json  浏览器加载：{model,dim, items:[{h,t,u,s,tp,v(base64 int8)}]}

向量做 int8 量化（句向量已 L2 归一化，分量在 ~[-1,1]，*127 取整）。
浏览器侧用【完全相同的】Xenova/multilingual-e5-small 量化模型嵌入查询，
反量化后算余弦即可——离线/在线同模型，向量可比。

嵌入文本：标题 + 中文总结 + 关键词 + 主题；缺摘要的(无有效总结)用
标题 + 主题 兜底（避免把"暂无摘要"占位文案嵌进去污染）。
"""

import base64
import json
import logging
import sys
from pathlib import Path

import numpy as np

for s in (sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8")

from main import load_papers
from src.dedup import hash_paper
from src.embed import EMB_DIM, embed_passages

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("idx")

_ROOT = Path(__file__).resolve().parent
_EMB_PATH = _ROOT / "data" / "embeddings.json"
_IDX_PATH = _ROOT / "docs" / "search-index.json"
_BATCH = 64


def _doc_text(p: dict) -> str:
    title = (p.get("title") or "").strip()
    topic = (p.get("topic") or "").strip()
    if p.get("abstract_missing"):           # 总结是占位文案，别嵌进去
        return f"{title}. 主题: {topic}"
    summary = (p.get("summary") or "").strip()
    kws = "、".join(p.get("keywords") or [])
    return f"{title}. {summary} 关键词: {kws}. 主题: {topic}"


def main() -> None:
    papers = load_papers()
    emb: dict[str, list[int]] = {}
    if _EMB_PATH.exists():
        try:
            emb = json.loads(_EMB_PATH.read_text(encoding="utf-8"))
        except Exception:
            emb = {}
    log.info("归档 %d 篇；已存向量 %d 条", len(papers), len(emb))

    # 只对还没向量的论文算（增量 / 断点续传）
    todo = [(hash_paper(p), p) for p in papers]
    todo = [(h, p) for h, p in todo if h not in emb]
    log.info("待嵌入 %d 篇", len(todo))

    for i in range(0, len(todo), _BATCH):
        chunk = todo[i:i + _BATCH]
        vecs = embed_passages([_doc_text(p) for _, p in chunk])
        q = np.clip(np.round(vecs * 127), -127, 127).astype(np.int8)
        for (h, _), row in zip(chunk, q):
            emb[h] = row.tolist()
        if (i // _BATCH) % 5 == 0 or i + _BATCH >= len(todo):
            _EMB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _EMB_PATH.write_text(json.dumps(emb), encoding="utf-8")
            log.info("checkpoint：已嵌入 %d/%d", min(i + _BATCH, len(todo)), len(todo))

    _EMB_PATH.write_text(json.dumps(emb), encoding="utf-8")

    # 生成浏览器索引：每篇一条（int8 向量 base64 打包）
    items = []
    for p in papers:
        h = hash_paper(p)
        v = emb.get(h)
        if not v:
            continue
        b = base64.b64encode(np.array(v, dtype=np.int8).tobytes()).decode("ascii")
        items.append({
            "h": h[:12],
            "t": p.get("title") or "",
            "u": p.get("url") or "",
            "s": p.get("source") or "",
            "tp": p.get("topic") or "",
            "v": b,
        })
    _IDX_PATH.parent.mkdir(parents=True, exist_ok=True)
    _IDX_PATH.write_text(json.dumps(
        {"model": "Xenova/multilingual-e5-small", "dim": EMB_DIM,
         "count": len(items), "items": items},
        ensure_ascii=False), encoding="utf-8")
    size_mb = _IDX_PATH.stat().st_size / 1e6
    log.info("完成：向量 %d 条；docs/search-index.json %d 篇，%.2f MB",
             len(emb), len(items), size_mb)


if __name__ == "__main__":
    main()
