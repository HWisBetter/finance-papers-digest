// 语义搜索（前端）。
// 关键：模型必须与离线嵌入用的同一个 ONNX（Xenova/multilingual-e5-small 量化版），
// 否则向量不可比、检索失效。
//
// 本地预览：先把模型下载到 docs/models/，再启动静态服务器：
//   python -m http.server 8000（在 docs/ 目录），然后开 http://localhost:8000。
// GitHub Pages 部署：本地无模型时自动从 HuggingFace CDN 加载（首次 ~30MB，之后浏览器缓存）。

import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.2";

env.allowRemoteModels = true;        // GitHub Pages 无本地模型时回退到 HF CDN
env.allowLocalModels = true;
env.localModelPath = "models/";      // 本地优先：docs/models/Xenova/multilingual-e5-small/...

const $ = id => document.getElementById(id);
let pipe = null, index = null;

// 作者分词匹配：把查询按空格拆词，每个词都出现在作者名里（子串）即命中
const AUTHORS = JSON.parse(document.getElementById('author-index')?.textContent || '[]');
function matchAuthors(q) {
  const tokens = q.toLowerCase().trim().split(/\s+/).filter(t => t.length >= 1);
  if (!tokens.length) return [];
  return AUTHORS.filter(a => {
    const name = a.name.toLowerCase();
    return tokens.every(t => name.includes(t));
  });
}

async function ensureReady() {
  if (!pipe) {
    setStatus("首次加载语义模型 ~118MB（之后缓存秒开）…");
    pipe = await pipeline("feature-extraction", "Xenova/multilingual-e5-small",
                          { quantized: true });
  }
  if (!index) {
    setStatus("加载语料索引…");
    const r = await fetch("search-index.json");
    const data = await r.json();
    // base64 int8 -> Float32 + L2 归一化（量化后再归一化更稳）
    index = data.items.map(it => {
      const bin = atob(it.v);
      const v = new Float32Array(data.dim);
      let s = 0;
      for (let i = 0; i < data.dim; i++) {
        const x = ((bin.charCodeAt(i) << 24) >> 24) / 127;  // 解 int8
        v[i] = x; s += x * x;
      }
      const n = Math.sqrt(s) || 1;
      for (let i = 0; i < data.dim; i++) v[i] /= n;
      // [DeepSeek-Patch: include authors and abstract fields]
      return { t: it.t, u: it.u, s: it.s, tp: it.tp, a: it.a || '', ab: it.ab || '', v };
    });
  }
  setStatus("");
}

function setStatus(msg) { const el = $("search-status"); if (el) el.textContent = msg || ""; }

// 把 e5 余弦(集中在 0.72-0.90)映射成更直观的"相关度 %"
// [DeepSeek-Patch: adjust hi threshold to 0.95]
function scoreToPct(cos) {
  const lo = 0.72, hi = 0.95;
  return Math.round(Math.max(0, Math.min(1, (cos - lo) / (hi - lo))) * 100);
}

const esc = s => (s || "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// [DeepSeek-Patch: add tierBadge helper]
function tierBadge(pct) {
  if (pct >= 85) return '<span class="badge sim tier-high" title="高度相关">相关度 ' + pct + '%</span>';
  if (pct >= 60) return '<span class="badge sim tier-mid" title="相关">相关度 ' + pct + '%</span>';
  return '<span class="badge sim tier-low" title="较相关">相关度 ' + pct + '%</span>';
}

function renderResults(query, top) {
  const total = index ? index.length : 0;
  const authorHits = matchAuthors(query);
  const authorSection = authorHits.length ? `
    <h2 class="sec-title" style="font-size:15px;margin:0 0 8px">👤 作者快速跳转</h2>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px">
      ${authorHits.map(a => `
        <a href="${esc(a.href)}" class="author-chip">
          ${esc(a.name)} <span style="opacity:.7;font-size:11px">→ 查看全部论文</span>
        </a>`).join("")}
    </div>` : '';
  const html = `
    <h1 class="sec-title">🔍 搜索结果
      <span class="sec-sub">（"${esc(query)}"，按语义相关度排序）</span></h1>
    ${authorSection}
    <p class="count">${top.length ? `Top ${top.length} / 全库 ${total} 篇` : "未找到相关论文"}</p>
    ${top.map(r => `
      <article class="card">
        <h2 class="title"><a href="${esc(r.u)}" target="_blank" rel="noopener">${esc(r.t)}</a></h2>
        <div class="meta">
          <span class="badge src-${esc(r.s)}">${esc(r.s)}</span>
          ${r.tp ? `<span class="badge topic-badge">${esc(r.tp)}</span>` : ''}
          ${tierBadge(r.pct)}
        </div>
        ${r.a ? `<p class="card-authors">${esc(r.a)}</p>` : ''}
        ${r.ab ? `<p class="card-abstract">${esc(r.ab)}...</p>` : ''}
      </article>`).join("")}`;
  $("search-results").innerHTML = html;
  $("search-results").style.display = "";
  $("home-main").style.display = "none";
}

async function doSearch() {
  const q = ($("search-box").value || "").trim();
  if (!q) return clearResults();
  $("search-go").disabled = true;
  try {
    await ensureReady();
    setStatus("计算中…");
    const out = await pipe("query: " + q, { pooling: "mean", normalize: true });
    const qv = out.data;
    const scored = new Array(index.length);
    for (let k = 0; k < index.length; k++) {
      const v = index[k].v; let s = 0;
      for (let i = 0; i < qv.length; i++) s += qv[i] * v[i];
      scored[k] = { ...index[k], cos: s, pct: scoreToPct(s) };
    }
    scored.sort((a, b) => b.cos - a.cos);
    setStatus("");
    renderResults(q, scored.slice(0, 30));
  } catch (e) {
    console.error(e);
    setStatus("搜索失败: " + (e?.message || e));
  } finally {
    $("search-go").disabled = false;
  }
}

function clearResults() {
  $("search-results").innerHTML = "";
  $("search-results").style.display = "none";
  $("home-main").style.display = "";
  $("search-box").value = "";
  setStatus("");
}

$("search-go").addEventListener("click", doSearch);
$("search-box").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
$("search-clear").addEventListener("click", clearResults);

// [DeepSeek-Patch: auto-trigger search from URL param ?q=]
document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  if (q) {
    const box = $('search-box');
    if (box) { box.value = q; doSearch(); }
  }
});
// [DeepSeek-Patch end]
