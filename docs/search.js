// 搜索模块：语义搜索（桌面）+ 关键词搜索后备（iPhone / 模型加载失败时自动切换）
//
// 加载策略：
//   keyword_index.json (~2MB)  —— 页面加载时静默预取，随时可用
//   search-index.json + 118MB ONNX 模型 —— 点击搜索时按需加载
//
// iOS Safari 有 50MB 缓存上限，无法缓存 118MB 模型，自动走关键词模式。
// 搜索库自托管于 docs/vendor/transformers/；模型经 ModelScope 国内镜像加载。

import { pipeline, env } from "./vendor/transformers/transformers.min.js";

env.allowRemoteModels = true;
env.allowLocalModels  = true;
env.localModelPath    = "models/";
env.remoteHost        = "https://modelscope.cn/models/";
env.backends.onnx.wasm.wasmPaths = "vendor/transformers/";

const $ = id => document.getElementById(id);
const esc = s => (s || "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);

// ── 作者跳转（原有功能，不变）──────────────────────────────────────────────
const AUTHORS = JSON.parse(document.getElementById('author-index')?.textContent || '[]');
function matchAuthors(q) {
  const tokens = q.toLowerCase().trim().split(/\s+/).filter(t => t.length >= 1);
  if (!tokens.length) return [];
  return AUTHORS.filter(a => tokens.every(t => a.name.toLowerCase().includes(t)));
}

// ── 关键词索引：静默预取 ────────────────────────────────────────────────────
let kwIndex = null;
fetch("keyword_index.json").then(r => r.json()).then(d => { kwIndex = d; }).catch(() => {});

function keywordSearch(q) {
  if (!kwIndex) return [];
  const tokens = q.toLowerCase().split(/\s+/).filter(t => t.length >= 1);
  if (!tokens.length) return [];
  const scored = kwIndex.map(p => {
    const text = [p.t, p.ab, p.a, (p.kw || []).join(" ")].join(" ").toLowerCase();
    const hits = tokens.filter(t => text.includes(t));
    const titleHits = tokens.filter(t => (p.t || "").toLowerCase().includes(t));
    return { ...p, _h: hits.length, _th: titleHits.length };
  }).filter(x => x._h > 0);
  scored.sort((a, b) => b._th - a._th || b._h - a._h);
  return scored.slice(0, 30);
}

// ── 语义索引：按需加载 ──────────────────────────────────────────────────────
let pipe = null, semIndex = null;

function scoreToPct(cos) {
  return Math.round(Math.max(0, Math.min(1, (cos - 0.72) / (0.95 - 0.72))) * 100);
}
function tierBadge(pct) {
  if (pct >= 85) return `<span class="badge sim tier-high">相关度 ${pct}%</span>`;
  if (pct >= 60) return `<span class="badge sim tier-mid">相关度 ${pct}%</span>`;
  return `<span class="badge sim tier-low">相关度 ${pct}%</span>`;
}

async function ensureSemantic() {
  if (!pipe) {
    setStatus("加载语义模型 ~118MB（之后缓存秒开）…");
    pipe = await pipeline("feature-extraction", "Xenova/multilingual-e5-small", { quantized: true, revision: "master" });
  }
  if (!semIndex) {
    setStatus("加载语义索引…");
    const data = await fetch("search-index.json").then(r => r.json());
    semIndex = data.items.map(it => {
      const bin = atob(it.v);
      const v = new Float32Array(data.dim);
      let s = 0;
      for (let i = 0; i < data.dim; i++) {
        const x = ((bin.charCodeAt(i) << 24) >> 24) / 127;
        v[i] = x; s += x * x;
      }
      const n = Math.sqrt(s) || 1;
      for (let i = 0; i < data.dim; i++) v[i] /= n;
      return { t: it.t, u: it.u, s: it.s, tp: it.tp, a: it.a || "", ab: it.ab || "", v };
    });
  }
  setStatus("");
}

// ── 渲染 ───────────────────────────────────────────────────────────────────
function setStatus(msg) { const el = $("search-status"); if (el) el.textContent = msg || ""; }

function renderResults(q, items, mode, phase) {
  // mode: "semantic" | "keyword"  ·  phase: "final" | "pending"
  const total = mode === "semantic" ? (semIndex?.length || 0) : (kwIndex?.length || 0);
  const authorHits = matchAuthors(q);

  let noteText = "";
  if (mode === "keyword") {
    if (phase === "pending") {
      noteText = "⏳ 当前为关键词匹配结果，语义模型正在后台加载（~118MB，首次访问稍慢），加载完成后自动升级";
    } else if (isIOS) {
      noteText = "⌨️ iPhone 使用关键词模式（语义模型 ~118MB 超出 Safari 缓存限制）";
    } else {
      noteText = "⌨️ 关键词模式（语义模型加载失败或网络中断，已自动切换）";
    }
  }
  const modeNote = noteText ? `<p class="count kw-note">${noteText}</p>` : "";

  const authorSection = authorHits.length ? `
    <h2 class="sec-title" style="font-size:15px;margin:0 0 8px">👤 作者快速跳转</h2>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px">
      ${authorHits.map(a => `<a href="${esc(a.href)}" class="author-chip">
        ${esc(a.name)} <span style="opacity:.7;font-size:11px">→ 查看全部论文</span></a>`).join("")}
    </div>` : "";

  const label = mode === "semantic" ? "按语义相关度排序" : "按关键词匹配排序";
  const html = `
    <h1 class="sec-title">🔍 搜索结果
      <span class="sec-sub">（"${esc(q)}"，${label}）</span></h1>
    ${modeNote}${authorSection}
    <p class="count">${items.length ? `Top ${items.length} / 全库 ${total} 篇` : "未找到相关论文"}</p>
    ${items.map(r => `
      <article class="card">
        <h2 class="title"><a href="${esc(r.u)}" target="_blank" rel="noopener">${esc(r.t)}</a></h2>
        <div class="meta">
          <span class="badge src-${esc(r.s)}">${esc(r.s)}</span>
          ${r.tp ? `<span class="badge topic-badge">${esc(r.tp)}</span>` : ""}
          ${mode === "semantic" ? tierBadge(r.pct) : '<span class="badge kw-badge">关键词</span>'}
        </div>
        ${r.a  ? `<p class="card-authors">${esc(r.a)}</p>`   : ""}
        ${r.ab ? `<p class="card-abstract">${esc(r.ab)}…</p>` : ""}
      </article>`).join("")}`;

  $("search-results").innerHTML = html;
  $("search-results").style.display = "";
  $("home-main").style.display = "none";
}

// ── 主搜索入口 ─────────────────────────────────────────────────────────────
async function doSearch() {
  const q = ($("search-box").value || "").trim();
  if (!q) return clearResults();
  $("search-go").disabled = true;

  try {
    if (isIOS) {
      // iPhone：直接关键词模式，不尝试下载模型
      setStatus("");
      renderResults(q, keywordSearch(q), "keyword", "final");
      return;
    }

    // 桌面：先即时展示关键词结果（标注"加载中"），再在后台加载语义模型升级
    const kwResults = keywordSearch(q);
    if (kwResults.length || matchAuthors(q).length) {
      renderResults(q, kwResults, "keyword", "pending");
      setStatus("正在加载语义模型，加载完成后自动升级为语义搜索结果…");
    }

    try {
      await ensureSemantic();
      const out = await pipe("query: " + q, { pooling: "mean", normalize: true });
      const qv = out.data;
      const scored = semIndex.map(r => {
        let s = 0;
        for (let i = 0; i < qv.length; i++) s += qv[i] * r.v[i];
        return { ...r, cos: s, pct: scoreToPct(s) };
      });
      scored.sort((a, b) => b.cos - a.cos);
      setStatus("");
      renderResults(q, scored.slice(0, 30), "semantic", "final");
    } catch (e) {
      console.warn("语义搜索失败，保留关键词结果:", e);
      setStatus("");
      renderResults(q, keywordSearch(q), "keyword", "final");
    }
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

// iOS：更新搜索框 placeholder 和提示
if (isIOS) {
  const box = $("search-box");
  if (box) box.placeholder = box.placeholder.replace("按语义检索", "关键词搜索");
  setStatus("iPhone 使用关键词搜索模式（全库可用）");
}

// URL 参数触发搜索
document.addEventListener("DOMContentLoaded", () => {
  const q = new URLSearchParams(window.location.search).get("q");
  if (q) { const box = $("search-box"); if (box) { box.value = q; doSearch(); } }
});
