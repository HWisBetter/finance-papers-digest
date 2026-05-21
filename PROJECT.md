# finance-papers-digest · 项目档案

> 自动抓取金融顶刊+NBER+SSRN 论文，AI 总结，渲染成静态站，每天更新。
> 在线：https://hwisbetter.github.io/finance-papers-digest/

本文档为**项目复现/修改/排错**的完整地图。配合代码注释和 CLAUDE.md（架构师工作流）一起看。

---

## 1. 它是什么

一个**个人金融论文聚合站**：
- **来源**：JF / JFE / RFS / JFQA / JAR（5 顶刊）+ MS（管理学）+ NBER（工作论文）+ SSRN（关注大佬的 working paper）
- **流程**：每天 GitHub Actions 自动抓取→AI 总结→渲染静态 HTML→提交回仓库→GitHub Pages 部署
- **特色**：大佬前瞻（关注 100+ 学者）、发文活跃榜（数据驱动 h-index）、语义+关键词双模式搜索

整个站点是**纯静态**，浏览器无任何服务端依赖。

---

## 2. 整体架构（文字图）

```
┌─────────────── 每日 (10:00 北京) ───────────────┐
│                                                  │
│  GitHub Actions cron                             │
│        ↓                                         │
│  main.py                                         │
│  ┌────────────────────────────────────────┐     │
│  │ 1. load papers.json + seen.json        │     │
│  │ 2. fetch 8 sources (并行式失败隔离)     │     │
│  │ 3. dedup 过滤新论文                     │     │
│  │ 4. enrich 缺摘要 (Semantic Scholar)    │     │
│  │ 4b. NBER RSS 摘要 backfill             │     │
│  │ 5. 盖日期章 (first_seen=today)         │     │
│  │ 6. DeepSeek 总结 (逐篇)                │     │
│  │ 7. 复活 abstract_missing 老论文        │     │
│  │ 8. 写回 papers.json + seen.json        │     │
│  │ 9. annotate_all (高亮 AI/大佬)         │     │
│  │ 10. render() 生成 docs/                │     │
│  └────────────────────────────────────────┘     │
│        ↓                                         │
│  build_search_index.py                           │
│        ↓                                         │
│  git commit + push → Pages 自动部署              │
│                                                  │
└──────────────────────────────────────────────────┘

┌─────────────── 每月 1 号 11:00 ──────────────────┐
│  refresh_citations.py (OpenAlex 拉引用计数)       │
│        ↓                                          │
│  写回 papers.json + 渲染 + push                  │
└──────────────────────────────────────────────────┘
```

---

## 3. 目录结构

```
finance-papers-digest/
├── CLAUDE.md              # AI 架构师工作流（外包给 DeepSeek 的规则）
├── PROJECT.md             # 本文档
├── README.md              # (可选) 公开介绍
├── main.py                # 流水线主入口
├── build_search_index.py  # 计算语义向量 + 写 docs/search-index.json
├── refresh_citations.py   # 月度引用刷新
├── backfill_run.py        # 一次性历史回填脚本
├── filter_ms_nber.py      # 一次性 MS/NBER 相关性清洗脚本
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量模板
├── .env                   # 真实 API key（.gitignore）
│
├── config/
│   ├── sources.yaml       # 数据源开关/参数
│   └── highlight.yaml     # 关注大佬 + AI 关键词
│
├── data/
│   ├── papers.json        # 全量论文归档（核心数据）
│   ├── seen.json          # 论文指纹集合（去重用）
│   └── embeddings.json    # 增量向量缓存
│
├── docs/                  # GitHub Pages 部署目录
│   ├── index.html         # 首页（7 天滚动 + 期刊入口）
│   ├── featured.html      # 大佬前瞻
│   ├── leaderboard.html   # 发文活跃榜
│   ├── author_*.html      # 每位 Top 学者的独立页
│   ├── JF.html / JFE.html / ...  # 每期刊一页
│   ├── style.css          # 样式（手维护，渲染器不覆盖）
│   ├── search.js          # 搜索前端（双模式）
│   ├── search-index.json  # 语义向量（~118MB）
│   └── keyword_index.json # 关键词索引（~2MB）
│
├── templates/             # Jinja2 模板
│   ├── base.html.j2       # 全站骨架（含 favicon）
│   ├── _macros.html.j2    # paper_card 论文卡片
│   ├── index.html.j2      # 首页
│   ├── featured.html.j2   # 大佬前瞻
│   ├── leaderboard.html.j2
│   ├── journal.html.j2    # 期刊页
│   └── author.html.j2     # 学者页
│
├── src/
│   ├── fetchers/
│   │   ├── nber.py        # NBER RSS（+ backfill_nber_abstracts）
│   │   ├── wiley.py       # Wiley RSS（JF / JAR）
│   │   ├── cambridge.py   # Cambridge Core RSS（JFQA）
│   │   ├── crossref.py    # Crossref API（RFS / JFE / MS）
│   │   └── ssrn.py        # OpenAlex API（按 author OpenAlex ID）
│   ├── sources.py         # YAML 调度 → 分派 fetcher
│   ├── dedup.py           # SHA256(title + first_author) 指纹
│   ├── enrich.py          # Semantic Scholar 按 DOI 补摘要
│   ├── summarizer.py      # DeepSeek API 调总结
│   ├── highlight.py       # is_ai / is_featured 打标
│   ├── relevance.py       # MS/NBER 金融-AI 相关性过滤
│   ├── textnoise.py       # 摘要里 Crossref 噪音清洗
│   ├── embed.py           # ONNX multilingual-e5-small
│   ├── leaderboard.py     # h-index + log10(1+引用) 算分
│   ├── citations.py       # OpenAlex 拉 cited_by_count
│   ├── featured_candidates.py  # 候选大佬推荐
│   └── renderer.py        # Jinja2 渲染整站
│
└── .github/workflows/
    ├── daily_update.yml         # 每日 cron
    └── monthly_citations.yml    # 月度引用
```

---

## 4. 数据源（8 个）

每个源在 `config/sources.yaml` 里通过 `type` 字段决定走哪个 fetcher：

| 源 | type | 参数 | fetcher | 备注 |
|---|---|---|---|---|
| NBER | `nber_rss` | 无 | `src/fetchers/nber.py` | 工作论文，含 backfill |
| JF | `wiley_rss` | issn=15406261 | `src/fetchers/wiley.py` | |
| JAR | `wiley_rss` | issn=1475679X | 同上 | |
| JFQA | `cambridge_rss` | feed_url | `src/fetchers/cambridge.py` | |
| RFS | `crossref` | issn=1465-7368, rows=40 | `src/fetchers/crossref.py` | Oxford 反爬绕过 |
| JFE | `crossref` | issn=0304-405X, rows=40 | 同上 | Elsevier 无摘要时靠 enrich |
| MS | `crossref` | issn=1526-5501, rows=40 | 同上 | 全收，靠相关性过滤 |
| SSRN | `ssrn` | watch_authors[] | `src/fetchers/ssrn.py` | OpenAlex API |

**加新期刊**：只改 sources.yaml；新增出版商类型才需要在 `src/sources.py` 的 `fetch_source()` 里多一行 `if stype == "xxx":`。

**SSRN 大佬名单**：sources.yaml 里 `watch_authors` 用 `{name, openalex_id}` 配置；OpenAlex ID 从 https://openalex.org 搜作者拿（形如 `A5005696029`）。

---

## 5. 流水线步骤（main.py 详解）

```python
1.  load_seen() + load_papers()        # 读已见指纹 + 全量归档
2.  fetch_all()                        # 抓所有启用的源
3.  filter 新论文（指纹 + 相关性）
    - 指纹比对 seen.json
    - MS/NBER 多过一遍 is_finance_or_ai_relevant()
4.  enrich_missing_abstracts()         # Semantic Scholar 按 DOI 补
4b. backfill_nber_abstracts()          # 二次：NBER 当前 RSS 补摘要
5.  盖日期 + first_seen=today
6.  summarize_paper() × n              # DeepSeek 逐篇总结
7.  复活：归档里曾 abstract_missing 的，这次补到摘要的，重做总结
8.  by_hash 合并 → save_papers() + save_seen()
9.  annotate_all()                     # is_ai / is_featured / featured_matched
9b. is_new = (first_seen == today)     # 渲染期临时标记，不持久化
10. render() → docs/*.html + keyword_index.json
```

**容错原则**：
- 单源失败不影响其他源
- 单篇总结失败不写 seen，下次自动重试
- abstract_missing 的论文【可复活】，永远保留重试机会
- 一切失败都不会丢论文（"never drop a real article"）

---

## 6. AI 总结（src/summarizer.py）

- **模型**：DeepSeek（`deepseek-chat`，OpenAI 兼容接口）
- **环境变量**：`DEEPSEEK_API_KEY`（在 `.env` 或 GitHub Secrets）
- **风格要求**（保存在 memory）：
  - 中文一段话总结
  - 完整性 > 简洁性
  - 中英术语混排
  - 不要分点
- **失败策略**：tenacity 指数退避重试；最终失败不写归档，下次再试

如要换模型，改 `summarizer.py` 里的 `MODEL` 常量。**注意**：DeepSeek reasoner 模型 max_tokens 上限是 2000，不是 8000。

---

## 7. 高亮系统（src/highlight.py + config/highlight.yaml）

两类视觉标记，**只影响展示，不影响排序/筛选**：

1. **AI 论文**：标题/摘要/总结/关键词命中 `ai_keywords`（子串匹配）或 `ai_acronyms`（整词匹配）
2. **大佬论文**：作者命中 `featured_authors`（姓名归一化精确匹配）

**别名机制**：`featured_aliases` 把同一人多种写法合并到规范显示名。例如 `Bryan T. Kelly → Bryan Kelly`，名单和分组用规范名展示，但匹配时两种写法都生效。

**加大佬**：直接编辑 `config/highlight.yaml` 的 `featured_authors`，下次跑流水线自动生效，无需改代码。

---

## 8. 静态站点（src/renderer.py + templates/）

**模板继承**：
- `base.html.j2`：head + 站点头 + 导航 + 内容块 + 页脚
- 其余页面 `{% extends "base.html.j2" %}` 然后填 `{% block content %}`
- `_macros.html.j2` 里的 `paper_card` 宏全站共用

**页面类型**：
- `index.html`：首页，7 天滚动按日期分组 + 期刊入口卡片
- `featured.html`：大佬前瞻，跨期刊按学者分组（仅 SSRN）
- `leaderboard.html`：发文活跃榜，前 30
- `author_<name>.html`：每位榜单学者的独立页
- `<SOURCE>.html`：每期刊一页，按 topic 分组

**首页 7 天逻辑**（renderer.py 第 200 行附近）：
- 以最新 `first_seen` 为基准往前推 6 天
- 按 `first_seen` 分组展示，新→旧
- SSRN 只显示 `is_featured` 的

**Favicon**：base.html.j2 里一行 SVG inline（📈 emoji），不需要图片文件。

---

## 9. 搜索（docs/search.js + 双索引）

**双模式**：
- **语义模式**（桌面）：下载 ~118MB ONNX 模型（`Xenova/multilingual-e5-small`），余弦相似度排序
- **关键词模式**（iPhone/模型失败时）：纯字符串匹配，~2MB JSON

**索引文件**：
- `docs/search-index.json`：int8 量化向量 + 元数据（由 `build_search_index.py` 生成）
- `docs/keyword_index.json`：纯文本（标题/摘要/作者/关键词），由 `renderer.py` 每次渲染时生成

**iOS 检测**：search.js 通过 `navigator.userAgent` 判断，iPhone 直接走关键词模式，**不尝试下载模型**，避免 Safari 50MB 缓存限制。

**作者跳转**：搜索框输入作者名（分词子串匹配），结果上方显示快速跳转 chip，链到 `author_*.html`。

---

## 10. 活跃榜算法（src/leaderboard.py）

**得分公式**：`score = h-index + log₁₀(1 + total_citations)`

- **范围**：JF / JFE / RFS 自 ChatGPT 发布日（2022-11-30）至今
- **h-index**：本语料内（被引≥h 次的论文数 ≥ h）
- **被引来源**：OpenAlex（`refresh_citations.py` 月度更新）
- **同名风险**：未集成机构信息，重名学者可能合并（如 Kai Li，目前主动排除）

榜单 Top-30 写入首页 widget 和独立的 leaderboard.html。每位上榜学者自动生成独立 `author_<name>.html`。

---

## 11. 引用数刷新（refresh_citations.py + 月度 workflow）

- **触发**：每月 1 号 03:00 UTC（11:00 北京），或手动 `workflow_dispatch`
- **流程**：遍历 JF/JFE/RFS 论文 → OpenAlex 按 DOI 查 `cited_by_count` → 写回 papers.json → 重渲染 → push
- **OpenAlex 无 key**，免费匿名使用，礼貌限速

---

## 12. NBER 摘要 backfill（src/fetchers/nber.py）

**问题**：NBER 有时先发 RSS 再补摘要，初次抓取可能拿到 `abstract_missing=True`。

**机制**：`backfill_nber_abstracts(papers)` 每次运行重拉当前 RSS，对在 RSS 窗口内且仍 abstract_missing 的论文，用新拿到的摘要更新。

**局限**：超出 RSS 窗口（约最近 50-100 篇）的老论文无法补，因为 NBER 页面是 JS 渲染，纯 HTTP 抓不到。如需补这部分，需接入 Playwright 等无头浏览器（目前未做，性价比低）。

---

## 13. 数据文件格式

### data/papers.json
```json
[
  {
    "id": "w35216",                    // 源内稳定 ID
    "title": "...",
    "authors": ["..."],
    "abstract": "...",
    "url": "https://...",
    "published_date": "2026-05-18",    // 真实发表日（YYYY-MM-DD）
    "first_seen": "2026-05-18",        // 本站首次抓到的日期
    "source": "NBER",                  // 来源标识
    "summary": "...",                  // DeepSeek 中文总结
    "keywords": ["..."],               // AI 总结时抽的关键词
    "ai_comment": "...",               // AI 点评
    "topic": "...",                    // 主题（用于期刊页分组）
    "abstract_missing": false,         // True 表示摘要缺失（仅展示标题/作者）
    "is_retracted": false,             // 撤稿标记
    "cited_by_count": 12               // OpenAlex 月度刷新
  }
]
```

**注意**：`is_ai` / `is_featured` / `featured_matched` 不在 papers.json，由 `annotate_all()` 渲染时计算。

### data/seen.json
```json
["sha256_hex_1", "sha256_hex_2", ...]
```
SHA256 over `normalize(title) + 0x1F + normalize(first_author)`。

### data/embeddings.json
```json
{ "sha256_hex": [int8_value_1, ..., int8_value_384], ... }
```
增量缓存，build_search_index.py 只对没向量的论文算。

---

## 14. GitHub Actions 工作流

### .github/workflows/daily_update.yml
- **cron**：`0 2 * * *`（02:00 UTC = 10:00 北京）
- **secrets 依赖**：`DEEPSEEK_API_KEY`，可选 `ELSEVIER_API_KEY`
- **步骤**：checkout → install → `python main.py` → `python build_search_index.py` → commit + push

### .github/workflows/monthly_citations.yml
- **cron**：`0 3 1 * *`（每月 1 号 03:00 UTC）
- **步骤**：checkout → install → `python refresh_citations.py` → commit + push

**手动触发**：两个 workflow 都有 `workflow_dispatch`，在 GitHub Actions 页可点 "Run workflow"。

---

## 15. 本地开发环境（Windows 重点！）

### 必备
1. **Python 3.11** 装在 `.venv/`
2. **必须**用 `.venv/Scripts/python.exe`，不能用系统 `python`（Windows Store 桩，exit 49）
3. `pip install -r requirements.txt`
4. 复制 `.env.example` → `.env`，填 `DEEPSEEK_API_KEY`

### 编码
- Windows 控制台默认 GBK，打印中文会崩
- `main.py` 已经在启动时强制 `sys.stdout.reconfigure(encoding="utf-8")`
- 写 Python 脚本时第一行加同样的代码，否则随时崩

### 常用命令
```powershell
# 跑完整流水线
.venv/Scripts/python.exe main.py

# 仅渲染（不抓新数据）
.venv/Scripts/python.exe -c "import json; from pathlib import Path; from src.highlight import annotate_all; from src.renderer import render; papers = json.loads(Path('data/papers.json').read_text(encoding='utf-8')); render(annotate_all(papers), Path('docs'))"

# 重建语义索引（耗时长）
.venv/Scripts/python.exe build_search_index.py

# 冒烟测试：只处理前 3 篇新论文
$env:MAX_NEW_PAPERS=3; .venv/Scripts/python.exe main.py
```

### Aider 外包注意事项
- 路径：`C:\Users\SanMs\AppData\Roaming\Python\Python311\Scripts\aider.exe`
- **必须**加 `--no-pretty`，否则 GBK 终端崩
- 消息含 `${}` 时用 `--message-file <file>`，不能直接 `--message`

---

## 16. 常用扩展操作

### 加一个新期刊（同出版商）
1. 在 `config/sources.yaml` 加一条 `{name, type, issn/feed_url, enabled: true}`
2. 不动代码

### 加一个新出版商
1. 在 `src/fetchers/` 加新文件 `xxx.py`，实现 `fetch_xxx() -> list[dict]`
2. 在 `src/sources.py` 的 `fetch_source()` 加分派分支
3. 在 `config/sources.yaml` 用新 type

### 加一个关注大佬
1. 编辑 `config/highlight.yaml` 的 `featured_authors:` 列表
2. 如果想让他的 SSRN working paper 也被抓：在 `config/sources.yaml` 的 SSRN `watch_authors:` 加一条 `{name, openalex_id}`

### 修改 AI 关键词
- 直接编辑 `config/highlight.yaml` 的 `ai_keywords` / `ai_acronyms`

### 调整首页天数窗口
- `src/renderer.py` 第 200 行附近：`timedelta(days=6)` 改成想要的天数 - 1

### 改 AI 总结风格
- `src/summarizer.py` 里的 prompt
- 然后**清掉 summary 重跑**：`.venv/Scripts/python.exe -c "import json; from pathlib import Path; p=Path('data/papers.json'); ps=json.loads(p.read_text(encoding='utf-8')); [x.pop('summary',None) for x in ps]; p.write_text(json.dumps(ps,ensure_ascii=False,indent=2),encoding='utf-8')"`
- 注意会重新调用 DeepSeek API（要钱）

---

## 17. 已知坑（血泪教训）

| 坑 | 原因 | 解法 |
|---|---|---|
| `python` 命令返回 exit 49 | Windows Store 桩 | 用 `.venv/Scripts/python.exe` |
| 打印中文 GBK 崩溃 | 控制台编码 | `sys.stdout.reconfigure(encoding='utf-8')` |
| DeepSeek reasoner `max_tokens` 报错 | reasoner 上限 2000 | 用 `deepseek-chat`，max=8000 |
| Aider `--message` 含 `$` 崩溃 | shell 变量展开 | 用 `--message-file <file>` |
| Aider 输出乱码 | 终端编码 | 加 `--no-pretty` |
| numpy 2.x + onnxruntime 报 DLL 错 | ABI 冲突 | 锁 `numpy<2`, `onnxruntime==1.17.3` |
| NBER 摘要时有时无 | RSS 时序问题 | `backfill_nber_abstracts()` 已自动重试 |
| NBER DOI URL 跳付费墙 | DOI 直连 PDF | 改用 `nber.org/papers/wXXXXX` |
| iOS 搜索"加载失败" | Safari 50MB 缓存限制 | 已加关键词模式后备 |
| GitHub Pages 缓存旧版 | CDN 缓存 | 用户 Ctrl+Shift+R 强刷 |
| SSRN 首批导入"洪水" 首页 | `first_seen` 都是同一天 | 已回拨到 2026-05-01 排除窗口外 |

---

## 18. 外部 API 速查

| API | 用途 | 需要 Key | 限速 |
|---|---|---|---|
| DeepSeek | AI 总结 | 是（`DEEPSEEK_API_KEY`） | 按 token 计费 |
| OpenAlex | SSRN 抓取 + 月度引用 | 否 | 匿名 100k/天 |
| Semantic Scholar | 摘要 enrich | 否 | 匿名 100/5min |
| CrossRef | RFS/JFE/MS RSS | 否 | 礼貌限速 |
| NBER RSS | 工作论文 | 否 | 仅 UA 头 |
| Wiley/Cambridge RSS | 期刊 feed | 否 | 仅 UA 头 |
| Elsevier (可选) | JFE 补摘要 | 是（`ELSEVIER_API_KEY`） | 不填则跳过 |

---

## 19. 部署（GitHub Pages）

1. 仓库 Settings → Pages → Source = `Deploy from a branch`
2. Branch = `main`, Folder = `/docs`
3. Workflow push 后自动部署（约 1 分钟）
4. URL：`https://<user>.github.io/<repo>/`

**自定义域名**：在 Settings → Pages → Custom domain 填入；DNS CNAME 指向 `<user>.github.io`。

**改私有**：免费 GitHub Pages 不支持私有仓库；要么 GitHub Pro ($4/月)，要么换 Cloudflare Pages（免费支持私有 GitHub）。

---

## 20. 复现项目（从零到部署）

```bash
# 1. 克隆 + 装依赖
git clone https://github.com/<user>/<repo>.git
cd <repo>
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -r requirements.txt

# 2. 配密钥
cp .env.example .env
# 编辑 .env 填 DEEPSEEK_API_KEY

# 3. 配 GitHub Secrets（部署到 Actions）
# Settings → Secrets → Actions → New repository secret
# 添加 DEEPSEEK_API_KEY

# 4. 首次运行（冒烟测试）
$env:MAX_NEW_PAPERS=5; .venv/Scripts/python.exe main.py
# 检查 docs/index.html 浏览器打开

# 5. 全量运行（首次会调用 200+ 次 DeepSeek API）
.venv/Scripts/python.exe main.py
.venv/Scripts/python.exe build_search_index.py

# 6. 启用 Pages
# Settings → Pages → Source = Deploy from a branch, main, /docs

# 7. 启用 Actions
# Actions 标签页 → 启用 workflows
# 之后每天 10:00 北京时间自动跑
```

---

## 21. 未来改进方向（备忘）

- [ ] **SSRN posted date 抓取**：目前因 SSRN 反爬未实现，所有 SSRN 论文日期显示已隐藏
- [ ] **NBER 历史摘要补全**：用 Playwright 渲染 JS 抓老论文摘要（48 篇缺失，性价比待评估）
- [ ] **同名学者消歧**：集成 OpenAlex 机构信息，让 Kai Li 等重名学者可以加入名单
- [ ] **AI 论文专题页**：把 `is_ai=True` 的论文聚到独立页
- [ ] **作者订阅 RSS**：每位大佬一个 RSS feed，让用户在 RSS 阅读器订阅
- [ ] **更细的 topic 主题分类**：目前 topic 由 AI 总结时给出，可以改成预定义分类
- [ ] **历史趋势图**：每月新论文数、活跃榜变化等

---

## 22. 重要文件随手查

| 想做什么 | 改哪个文件 |
|---|---|
| 加期刊源 | `config/sources.yaml` |
| 加关注大佬 | `config/highlight.yaml` |
| 改 AI 关键词 | `config/highlight.yaml` |
| 改首页布局 | `templates/index.html.j2` |
| 改论文卡片样式 | `templates/_macros.html.j2` + `docs/style.css` |
| 改 AI 总结风格 | `src/summarizer.py` 里的 prompt |
| 改总结模型 | `src/summarizer.py` 里的 `MODEL` 常量 |
| 改 cron 时间 | `.github/workflows/*.yml` |
| 加新出版商 | `src/fetchers/<new>.py` + `src/sources.py` 加分派 |
| 改活跃榜公式 | `src/leaderboard.py` |
| 改搜索模式逻辑 | `docs/search.js` |
| 改 NBER 抓取 | `src/fetchers/nber.py` |
| 改 MS/NBER 过滤词 | `src/relevance.py` |
| 改 favicon | `templates/base.html.j2` 里的 SVG |

---

**文档版本**：2026-05-21
**作者**：架构由 Claude Code 协助设计，DeepSeek 协助实现
