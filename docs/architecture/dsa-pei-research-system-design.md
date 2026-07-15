# DSA + Public Equity Investing 深度研究系统设计

> 状态：Implemented baseline（Phase 0–4）
>
> 首次设计基线：`50c8d0e`（`v3.23.0-6-g50c8d0e`）
>
> 最后更新：2026-07-15
>
> 文档性质：架构与需求设计；实际启用、运维和已知边界见 [`docs/pei-research.md`](../pei-research.md)

实现说明：2026-07-15 已按本设计完成首版模块化单体 + 独立 Worker 基线。与初稿相比，首版 Web 进度采用 10 秒轮询而不是新增 Research SSE；其余核心边界包括独立 Research DB、PIT、不可变 Evidence Pack、受限 MCP、持久化租约队列、服务端二次校验、单次/月度 Token 预算、人工审核、定时公告/告警触发和 opt-in 回滚均已实现。

## 1. 摘要

本设计将 daily_stock_analysis（下文简称 DSA）作为产品与监控外壳，将 OpenAI Public Equity Investing（下文简称 PEI）作为深度研究工作流引擎，并使用独立研究数据域、MCP 工具契约和外置 Worker 连接两者。

目标架构为：

> 模块化单体 DSA + 独立研究数据域 + 只读 MCP + 外置 PEI Worker

DSA 继续负责自选股、行情、技术指标、新闻、告警、组合、调度、通知和 Web/Desktop 展示。PEI 只处理成本更高、耗时更长、需要完整证据链的基本面研究任务，例如财报深挖、三表模型、DCF、可比估值、投资论点、催化剂和风险跟踪。

PEI 是 Codex/ChatGPT 中的插件工作流，不是可以被 DSA Python 进程直接导入的 SDK。因此，DSA 不直接依赖 PEI 内部实现，而是通过稳定的数据与任务契约协作：

- MCP 向 PEI 提供受控、可追溯的研究数据工具。
- 持久化任务队列管理深度研究生命周期。
- 外置 Worker 通过 `codex exec` 调用已安装的 PEI 工作流。
- Worker 对 PEI 输出执行 JSON Schema 校验，再写回 DSA。

相关官方概念：

- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex Plugins](https://learn.chatgpt.com/docs/plugins)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)
- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

## 2. 背景与当前基线

### 2.1 可以复用的现有能力

DSA 已经具备本设计需要的多数产品基础：

- FastAPI 应用和版本化路由，可新增独立 Research API，见 [`api/v1/router.py`](../../api/v1/router.py)。
- React Web、Electron Desktop 和报告展示组件。
- SSE 任务状态推送和运行流展示。
- 自选股、持仓、告警、通知和定时调度。
- 多行情源与 fallback。
- 技术分析、新闻检索、Agent Chat 和 DSA 自有 Deep Research。
- AlphaSift 外部能力集成，可作为可选依赖、状态诊断、任务轮询和降级设计的参考。
- Docker 与本地运行模式，见 [`docker/docker-compose.yml`](../../docker/docker-compose.yml)。

### 2.2 必须补齐的边界

现有任务队列在内存中保存任务状态，见 [`src/services/task_queue.py`](../../src/services/task_queue.py)。这适合普通分析和前端进度展示，但不适合可能运行数分钟至数十分钟、必须跨重启恢复的 PEI 深度研究任务。

现有 `/api/v1/agent/research` 使用 DSA 自己的 `ResearchAgent`，见 [`api/v1/endpoints/agent.py`](../../api/v1/endpoints/agent.py)。它不等同于 PEI，不应通过改名或替换现有接口来接入 PEI。

现有 `FundamentalSnapshot` 是运行时 JSON 快照，当前定位为 write-only，见 [`src/storage.py`](../../src/storage.py)。它可以继续用于兼容和运行审计，但不适合作为 point-in-time 财务事实库。

现有 Tushare fetcher 主要面向日行情。研究链路需要单独的 Tushare Research Provider，覆盖三张财务报表、财务指标、业绩预告、业绩快报、分红、股本和复权因子。

## 3. 设计假设

第一阶段按以下场景设计：

- 个人或小团队使用。
- 数十至数百只自选股。
- 日线、盘后和公告事件驱动。
- DSA 在本机或单台服务器运行，可使用 Docker。
- 最终投资决策由人完成，不自动下单。
- 允许深度研究任务在数分钟内异步完成。
- 首要目标是数据可追溯、结果可复现和系统可维护，而不是分钟级吞吐。

以下情况将触发重新评估：

- 转为多人 SaaS 或多租户部署。
- 需要分钟级实时行情和实时研究。
- 需要自动交易或券商订单接入。
- 同时运行大量 PEI Worker。
- 研究数据库出现持续写锁、查询延迟或单机容量问题。

## 4. 目标与非目标

### 4.1 目标

1. 在 DSA 中手动或自动创建 PEI 深度研究任务。
2. 使用 Tushare Pro 提供结构化财务数据，使用官方公告提供权威原始证据。
3. 每份报告记录数据截止时间、数据来源、模型、插件、工作流和转换版本。
4. 支持财报深挖、首次覆盖、估值、投资论点、催化剂和风险跟踪。
5. 深度任务可以跨 DSA/Worker 重启恢复。
6. 报告的核心财务数字可以追溯到具体事实或公告。
7. 数据缺失或质量不足时显式阻断或降级，不允许模型自行补数。
8. PEI 功能关闭或不可用时，不影响 DSA 原有分析链路。

### 4.2 非目标

第一阶段不实现：

- 全 A 股每天运行完整 PEI。
- 自动下单和订单执行。
- 高频或分钟级交易策略。
- Kafka、Celery 或多微服务体系。
- 默认引入向量数据库。
- 多租户权限、计费和公开数据分发。
- 替换 DSA 现有技术分析、Agent Chat 或 Deep Research。

## 5. 需求分级

### 5.1 Must Have

- DSA 可以创建、查询、取消深度研究任务。
- 研究任务及每次执行尝试持久化。
- Tushare 完整财务数据接入。
- 巨潮、上交所、深交所和北交所公告发现、下载、去重与归档。
- Point-in-time 财务事实模型。
- 明确记录原始价格、复权因子与价格口径。
- 版本化 Evidence Pack。
- PEI 通过只读 MCP 获取数据。
- PEI 输出符合版本化 JSON Schema。
- Worker 校验成功后才允许写回报告。
- 报告显示来源、数据截止时间、数据缺口和降级状态。
- 服务重启后任务可以继续或安全重试。
- 幂等提交，避免同一事件重复生成研究。

### 5.2 Should Have

- 财报发布自动触发 Earnings Deep Dive。
- 重大公告触发 Thesis Update。
- 研究报告版本对比。
- 催化剂和证伪条件跟踪。
- 估值区间与股价变化跟踪。
- 人工审核与发布状态。
- Token、耗时、失败率和数据覆盖率监控。
- 报告完成后复用 DSA 通知渠道推送摘要。

### 5.3 Could Have

- 组合级风险研究。
- 行业和可比公司批量研究。
- 公告语义检索与向量召回。
- 多 PEI Worker 并发。
- PostgreSQL 与对象存储部署。

## 6. 总体架构

```text
                         ┌──────────────────────┐
                         │ DSA Web / Desktop UI │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │ DSA FastAPI          │
                         │                      │
                         │ 自选股 / 技术分析    │
                         │ 告警 / 通知 / 组合   │
                         │ Research Center      │
                         └───────┬───────┬──────┘
                                 │       │
                  ┌──────────────▼─┐   ┌─▼────────────────┐
                  │ DSA Core DB    │   │ Research Domain  │
                  │ 现有 SQLite    │   │ research.db      │
                  └────────────────┘   └───────┬──────────┘
                                               │
             ┌─────────────────────────────────┼────────────────────┐
             │                                 │                    │
   ┌─────────▼─────────┐            ┌──────────▼─────────┐  ┌──────▼──────┐
   │ Tushare Research  │            │ Official Filings  │  │ Evidence    │
   │ Provider          │            │ CNINFO/SSE/SZSE   │  │ Pack Builder│
   └───────────────────┘            └────────────────────┘  └──────┬──────┘
                                                                   │
                                                        ┌──────────▼──────────┐
                                                        │ DSA Research MCP   │
                                                        │ 只读领域级工具      │
                                                        └──────────┬──────────┘
                                                                   │
                                                        ┌──────────▼──────────┐
                                                        │ Codex + PEI Worker │
                                                        │ Plugin workflows   │
                                                        └──────────┬──────────┘
                                                                   │
                                                        JSON Schema 验证
                                                                   │
                                                        ┌──────────▼──────────┐
                                                        │ Research Report    │
                                                        │ + Thesis/Catalyst │
                                                        └─────────────────────┘
```

## 7. 组件职责

### 7.1 DSA Product Shell

继续负责：

- 自选股与股票池。
- 行情、技术指标和技术信号。
- 新闻、情报、告警和通知。
- 组合和持仓上下文。
- 普通 DSA 分析与历史报告。
- Research Center、任务状态和报告展示。
- 深度研究触发规则。

DSA 不负责直接实现 PEI 的财务建模 Prompt，也不在 FastAPI 请求线程中同步运行 Codex。

### 7.2 Research Domain

新增独立研究数据域，负责：

- 证券主数据规范化。
- Tushare 结构化财务数据。
- 官方公告发现、下载、解析和版本管理。
- 财务事实、公司行动和价格口径。
- Evidence Pack 构建和质量门禁。
- 深度研究任务、执行记录、报告和证据关系。
- 投资论点与催化剂生命周期。

### 7.3 DSA Research MCP

只暴露领域级、参数化、可审计的读取工具。MCP 不直接访问 DSA Core DB，不提供任意 SQL、Shell 或 URL 请求能力。

初期使用本地 STDIO MCP 适配器，通过 DSA Research API 读取数据。出现多机器部署需求后，再升级为带 Bearer/OAuth 鉴权的 Streamable HTTP MCP。

### 7.4 PEI Worker

独立于 DSA Web/API 进程运行，负责：

- 领取持久化研究任务。
- 维护租约和 heartbeat。
- 准备独立工作目录。
- 调用 `codex exec` 和指定 PEI 工作流。
- 收集 JSON、Markdown、Excel 等产物。
- 校验输出 Schema、Evidence ID 和必要字段。
- 通过受控 Worker API 写回结果。
- 记录 Token、模型、插件版本、耗时和错误。

### 7.5 Research Center

新增独立页面，而不是继续扩展现有首页。页面包含：

- 待研究、运行中、数据阻塞、待审核和已发布任务。
- 公司研究时间线。
- 报告版本和差异。
- 投资论点、证伪条件和催化剂。
- 估值历史。
- 数据覆盖率、来源和警告。

## 8. 研究数据存储

### 8.1 存储边界

第一阶段建议使用：

```text
data/research/research.db
data/research/documents/
data/research/evidence-packs/
data/research/artifacts/
```

`research.db` 与现有 DSA SQLite 分离，但仍由同一个 FastAPI 应用进程中的 Research Service 管理。MCP 和 Worker 通过 API 访问，避免多个进程直接并发写 SQLite。

分离存储的原因：

- 研究数据模型和生命周期明显区别于普通分析历史。
- 官方公告、财务事实和报告版本会快速增长。
- 减少对 `src/storage.py` 的长期侵入和上游合并冲突。
- 未来只迁移 Research Domain 到 PostgreSQL，不要求同时迁移 DSA Core DB。

接受的代价：

- 两个数据库之间没有外键和跨库事务。
- 通过规范化证券 ID、任务 ID 和应用服务保证一致性。

### 8.2 核心数据表

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `security_master` | 统一证券身份 | `ts_code`、交易所、代码、名称、行业、上市状态 |
| `source_document` | 官方公告和财报 | 来源、外部 ID、发布时间、报告期、URL、文件路径、SHA256、修订关系 |
| `financial_fact` | 标准化财务事实 | 指标、报告期、值、单位、币种、公告时间、可用时间、来源、版本 |
| `corporate_action` | 分红、送转、回购、增发等 | 类型、登记日、除权日、每股金额、来源 |
| `market_price_basis` | 行情与复权口径 | 原始价格、复权因子、来源、口径 |
| `evidence_pack` | 一次研究的冻结输入 | 截止时间、Schema 版本、清单路径、哈希、质量状态 |
| `research_job` | 持久化任务 | 工作流、触发原因、状态、优先级、幂等键、租约、重试 |
| `research_run` | 每次执行尝试 | 模型、插件版本、Token、耗时、退出码、错误 |
| `research_report` | 结构化报告 | 报告类型、Markdown、结构化 JSON、审核状态、父版本 |
| `report_evidence` | 报告与证据关系 | 报告、公告、财务事实、引用位置 |
| `thesis_item` | 投资论点跟踪 | 论点、状态、置信度、证伪条件、下次检查时间 |
| `catalyst` | 催化剂日历 | 日期、概率、影响、状态、来源 |

### 8.3 关键索引

至少建立：

- `financial_fact(security_id, metric_code, period_end, available_at)`。
- `source_document(security_id, published_at)`。
- `source_document(source_name, external_id)` 唯一约束。
- `research_job(status, priority, created_at)`。
- `research_job(idempotency_key)` 唯一约束。
- `research_report(security_id, report_type, as_of)`。
- `report_evidence(report_id, evidence_type, evidence_id)`。

大文件和完整 Evidence Pack 不写入数据库 Text 字段，只保存路径、大小、哈希和摘要元数据。

## 9. Point-in-time 与 A 股数据语义

### 9.1 必要时间字段

`financial_fact` 至少包含：

```text
period_end         财务报告期
announced_at       公司或交易所正式披露时间
available_at       研究系统允许使用该数据的最早时间
ingested_at        系统实际抓取时间
revision_no        修订版本
source_record_id   原始数据记录
transform_version  单季度还原、单位转换等算法版本
```

历史研究必须按照：

```text
available_at <= research_as_of
```

选择当时可见的数据。修订数据采用 append-only，不覆盖旧版本。

### 9.2 A 股特殊口径

必须显式处理：

- 累计利润表转单季度。
- 年报、半年报和季报口径差异。
- 合并报表与母公司报表。
- 财务数据重述和修订。
- 元、千元、万元等单位转换。
- 财务报表币种与交易币种。
- 总股本与流通股本。
- 分红、送转、增发、回购和除权日。
- 原始价格、前复权和后复权。
- 退市、改名和证券代码迁移。
- Tushare `ann_date`、`f_ann_date`、`end_date`、`report_type` 和 `update_flag` 等字段。

## 10. 数据源与证据优先级

不同数据类型采用不同权威顺序：

```text
财务数字：
官方财报原文 > Tushare 结构化数据 > AkShare/网页聚合

公司事件：
交易所/巨潮公告 > 公司官网 > 权威媒体 > 搜索结果

行情：
统一口径行情源 + adj_factor
禁止在未声明时混用复权与不复权数据
```

数据源实现边界：

- `TushareResearchProvider`：证券主数据、三表、财务指标、预告、快报、分红、股本和复权因子。
- `OfficialDisclosureProvider`：巨潮、上交所、深交所和北交所公告。
- `ResearchNormalizer`：单位、币种、单季度、股本和修订处理。
- `EvidencePackBuilder`：冻结输入并运行质量门禁。

不要继续扩展现有日行情 Fetcher 以承载全部基本面职责，避免价格接口与研究数据接口混为一层。

## 11. Evidence Pack

每次 PEI 执行前冻结一个 Evidence Pack，不将数据库全部内容直接塞入 Prompt。

建议结构：

```json
{
  "schema_version": "1.0",
  "pack_id": "ep_xxx",
  "security": {},
  "as_of": "2026-07-15T18:00:00+08:00",
  "workflow": "earnings_deep_dive",
  "company_profile": {},
  "financials": {},
  "market_data": {},
  "corporate_actions": [],
  "filings": [],
  "news_events": [],
  "previous_research": [],
  "quality": {
    "status": "ready",
    "coverage": {},
    "warnings": [],
    "blocking_gaps": []
  },
  "manifest_hash": "sha256:..."
}
```

质量门禁按工作流定义：

- DCF：需要完整历史三表、股本、净债务和统一口径行情。
- Earnings Deep Dive：需要本期报告、上年同期和前一期对比数据。
- Thesis Update：允许财务数据未更新，但必须包含触发公告原文。
- Initiating Coverage：缺少关键三表时进入 `blocked_data`，不允许模型补数。

Evidence Pack 必须可重放。相同 Pack、相同工作流版本和相同模型配置应能复现输入边界，即使模型输出不是逐字确定的。

## 12. MCP 工具契约

### 12.1 建议工具

```text
resolve_security
get_company_profile
get_financial_statements
get_financial_indicators
get_market_history
get_corporate_actions
search_official_filings
get_filing_excerpt
get_news_events
get_previous_research
build_evidence_pack
get_evidence_pack_manifest
```

### 12.2 禁止工具

自动 PEI Worker 不提供：

```text
execute_sql
run_shell
fetch_any_url
write_any_file
update_database
```

### 12.3 统一响应信封

每个 MCP 工具统一返回：

```json
{
  "schema_version": "1.0",
  "as_of": "2026-07-15T18:00:00+08:00",
  "data_cutoff": "2026-07-15T17:59:00+08:00",
  "freshness": {},
  "coverage": {},
  "warnings": [],
  "citations": [],
  "payload": {}
}
```

单条财务事实应保留：

```json
{
  "value": 123456789.0,
  "unit": "CNY",
  "period_end": "2025-12-31",
  "available_at": "2026-03-28T18:32:00+08:00",
  "source": {
    "type": "tushare",
    "record_id": "...",
    "document_id": "...",
    "url": "..."
  },
  "quality": "verified"
}
```

### 12.4 传输选择

第一阶段选择 STDIO MCP：

- 适合本地自定义服务。
- 不额外开放网络端口。
- Codex 可以通过项目或用户 `config.toml` 配置。
- MCP 进程仅持有只读 Research API Token。

升级到 Streamable HTTP MCP 的触发条件：

- Worker 与 DSA 分布在不同机器。
- 多个用户或多个 Codex Host 需要共享服务。
- 已具备 TLS、OAuth/Bearer、审计和限流能力。

## 13. PEI Worker 与任务生命周期

### 13.1 执行流程

```text
DSA 创建 research_job
        ↓
Worker 领取任务并获得租约
        ↓
Research Service 构建 Evidence Pack
        ↓
Worker 调用 codex exec
        ↓
PEI 使用只读 MCP
        ↓
Worker 校验 JSON Schema 和 Evidence ID
        ↓
Worker 调用 DSA Research API 写回
        ↓
人工审核 / 发布 / 通知
```

### 13.2 状态机

```text
queued
  → collecting_data
  → data_ready
  → analyzing
  → validating
  → awaiting_review
  → published

异常状态：
blocked_data
failed_retryable
failed_permanent
cancel_requested
cancelled
```

### 13.3 Worker 约束

- 初始并发数为 1。
- 使用任务租约和 heartbeat，Worker 异常退出后任务可以重新领取。
- 使用幂等键避免同一股票、事件、工作流和版本重复提交。
- 设置工作流级超时和最大重试次数。
- 每次运行使用独立工作目录。
- 默认使用 `--ephemeral`。
- 只需要 JSON/Markdown 时使用只读 sandbox。
- 需要 Excel 等产物时，仅对独立产物目录开放 workspace write。
- 不在 DSA 源码仓库中运行带写权限的 PEI。
- 不使用 `danger-full-access`。
- 自动 Worker 的 MCP 工具必须使用 allowlist。

概念命令：

```bash
codex exec \
  --ephemeral \
  --sandbox read-only \
  --output-schema pei-report.schema.json \
  'Use $public-equity-investing to perform the requested workflow using only the configured DSA research MCP evidence.'
```

实施时不得依赖文档中的固定模型名。模型、插件和工作流版本应从受控配置读取并记录到 `research_run`。

## 14. PEI 工作流映射

| 触发条件 | PEI 工作流 | 主要输出 |
| --- | --- | --- |
| 首次覆盖公司 | Initiating Coverage | 完整投资报告、模型、估值、风险 |
| 财报发布 | Earnings Deep Dive | 超预期/低预期、驱动因素、预测调整 |
| 财报发布前 | Earnings Preview | 关注指标和情景分析 |
| 重大公告 | Thesis Tracker | 论点强化、削弱或证伪 |
| 事件日历变化 | Catalyst Calendar | 催化剂日期、概率和影响 |
| 估值显著变化 | DCF / Comps | 估值区间和敏感性 |
| DSA 异常技术信号 | Long/Short Pitch | 是否值得进一步研究 |
| 持仓风险变化 | Portfolio Risk Management | 仓位风险和对冲建议 |

运行原则：

- DSA 每天扫描全部自选股。
- PEI 只处理被事件、规则或人工选择触发的少数公司。
- 技术信号默认触发轻量研究候选，不直接触发完整首次覆盖。
- 同一公告或财报事件只生成一个幂等任务。

## 15. API 草案

本节为已实现的 v1 契约摘要；Worker 读取 Evidence Pack 的内部只读端点也位于同一版本前缀，完整运行说明见运维文档。

### 15.1 Web/UI API

```text
POST   /api/v1/research/jobs
GET    /api/v1/research/jobs/{job_id}
POST   /api/v1/research/jobs/{job_id}/cancel

GET    /api/v1/research/reports
GET    /api/v1/research/reports/{report_id}
POST   /api/v1/research/reports/{report_id}/review
GET    /api/v1/research/reports/{report_id}/evidence

GET    /api/v1/research/securities/{code}/timeline
GET    /api/v1/research/documents/{document_id}
```

### 15.2 Worker API

```text
POST /api/v1/research/worker/claim
POST /api/v1/research/worker/jobs/{job_id}/heartbeat
POST /api/v1/research/worker/jobs/{job_id}/complete
POST /api/v1/research/worker/jobs/{job_id}/fail
```

### 15.3 鉴权范围

Web 继续使用现有用户会话。Worker 使用独立 Bearer Token，不模拟浏览器 Cookie。

建议权限范围：

```text
research:data:read
research:job:claim
research:job:update
research:report:write
```

默认拒绝未声明权限。Worker Token 与 Tushare、Codex 凭据分离。

## 16. 报告输出契约

PEI 最终输出至少包含：

```json
{
  "schema_version": "1.0",
  "workflow": "earnings_deep_dive",
  "as_of": "2026-07-15T18:00:00+08:00",
  "security": {},
  "executive_summary": "",
  "thesis": [],
  "financial_analysis": {},
  "valuation": {},
  "catalysts": [],
  "risks": [],
  "invalidation_conditions": [],
  "data_gaps": [],
  "citations": [],
  "markdown": ""
}
```

校验要求：

- `as_of` 与 Evidence Pack 一致。
- 核心财务数字必须绑定 Evidence ID。
- 引用的 Evidence ID 必须存在于 Pack manifest。
- `data_gaps` 不能被空摘要掩盖。
- DCF/Comps 的关键假设、单位和币种必须明确。
- 无法验证的结论必须标记为推断。
- Schema 校验失败的输出保存为失败产物，不发布为正式报告。

## 17. 可观测性与成本控制

一条 `trace_id` 贯穿：

```text
DSA trigger
→ research_job
→ evidence_pack
→ research_run
→ Codex/PEI tool calls
→ research_report
→ notification
```

每次运行记录：

- 股票与工作流。
- 触发原因和源事件 ID。
- Evidence Pack ID、Schema 版本和哈希。
- 模型、插件和工作流版本。
- MCP Server 版本和工具调用摘要。
- 输入/输出 Token。
- 开始、结束和各阶段耗时。
- 重试次数和错误分类。
- 数据覆盖率和阻断项。
- 生成产物路径和哈希。

成本控制：

- 默认 Worker 并发为 1。
- 配置单次和月度 Token 预算。
- 同事件、同工作流、同版本幂等去重。
- 全市场扫描由 DSA 完成，PEI 只处理候选。
- 首次覆盖等高成本工作流默认人工触发。

## 18. 安全设计

### 18.1 凭据

- Tushare、Codex 和 Worker Token 只通过环境变量、系统钥匙串或受控密钥服务注入。
- 不在文档、配置样例、数据库或日志中写入真实 Token。
- DSA Docker 容器不挂载个人完整的 Codex Home。
- Worker 使用独立 Codex Home、插件安装和 MCP 配置。

### 18.2 Prompt Injection

官方公告、新闻和网页内容均视为不可信数据：

- MCP 响应明确标识文档内容不是系统指令。
- PEI 工作流不得执行公告或网页中的工具调用指令。
- 下载器只允许受信任域名和受控重定向。
- 不提供任意 URL 抓取工具。
- 公告原文与解析结果保留哈希，防止内容替换。

### 18.3 写入边界

- 自动 MCP 工具只读。
- PEI 不直接写数据库。
- Worker 校验结果后调用受控 API 写入。
- 报告发布与报告生成分离。
- 高风险写操作要求明确权限和审计记录。

## 19. 备份与恢复

需要备份：

- `research.db`。
- 官方公告原文件和解析产物。
- Evidence Pack manifest。
- 研究报告、模型和表格产物。
- Schema、工作流版本和迁移记录。

不备份真实密钥。恢复演练至少验证：

1. 数据库可恢复并通过迁移检查。
2. 文档哈希与数据库记录一致。
3. 历史 Evidence Pack 可以读取。
4. 失败或运行中的任务可以安全重置为可重试状态。
5. 已发布报告仍可访问其证据。

## 20. 测试策略

### 20.1 数据契约测试

- Tushare 响应 fixture 与字段映射。
- 官方公告列表、下载、重定向、哈希和重复数据。
- 单季度还原和单位转换。
- 财务修订 append-only。
- 原始、前复权和后复权口径。

### 20.2 Point-in-time 测试

- `as_of` 早于公告时，不得读取公告后的数据。
- 修订财报发布前仍读取旧版本。
- 同一 Evidence Pack 不因后续数据入库发生变化。

### 20.3 任务测试

- 幂等提交。
- 租约超时和任务重新领取。
- Worker 中途退出。
- 取消、重试和永久失败。
- DSA 重启后任务恢复。

### 20.4 MCP 契约测试

- 工具 Schema。
- 工具 allowlist。
- 无权限、超时、限流和数据缺失响应。
- 引用 ID 和 Evidence Pack 一致性。
- 禁止任意 SQL、Shell 和 URL。

### 20.5 PEI 集成测试

- 插件未安装或未启用。
- MCP 启动失败且被标记为 required。
- `codex exec` 超时。
- JSON Schema 校验失败。
- 报告包含不存在的 Evidence ID。
- 需要 Excel 时仅允许写任务产物目录。

## 21. 实施目录建议

为降低与上游 DSA 的合并冲突，优先新增文件并复用现有 Service/Repository 约定：

```text
api/v1/endpoints/research.py

src/research/
  models.py
  schemas.py
  repositories.py
  providers/
    tushare_research.py
    official_disclosure.py
  normalizer.py
  evidence_pack.py
  quality_gate.py

src/services/research_job_service.py
src/services/research_report_service.py

src/integrations/codex/
  mcp_server.py
  pei_runner.py
  output_validator.py

apps/dsa-web/src/pages/ResearchCenterPage.tsx
apps/dsa-web/src/api/research.ts
apps/dsa-web/src/types/research.ts

docs/architecture/
  dsa-pei-research-system-design.md
```

实际实现前应先确认 ORM Base、迁移入口和独立数据库初始化方式，避免产生第二套无法被应用生命周期管理的隐式初始化流程。

## 22. Architecture Decision Records

### ADR-001：采用模块化单体与外置 Worker

**状态：** Proposed

**上下文：** 当前目标是个人或小团队的日线/事件驱动系统，尚无独立扩容和大团队边界。

**决策：** DSA 保持模块化单体；PEI Worker 独立进程运行；不拆微服务。

**理由：**

- DSA 已有完整 FastAPI、调度、通知和前端。
- 深度研究和 Web 请求的资源、生命周期不同，Worker 独立可以避免阻塞 API。
- 微服务会提前引入服务发现、消息队列、分布式事务和部署复杂度。

**接受的取舍：**

- DSA 模块之间仍共享一个部署单元。
- Worker 与 DSA 通过 API 产生少量协议维护成本。

**重新评估条件：** 团队超过约十人、研究服务需要独立扩容，或出现多个独立部署团队。

### ADR-002：使用独立 Research SQLite

**状态：** Proposed

**上下文：** 研究数据和 DSA Core 数据的结构、规模、生命周期明显不同。

**决策：** 第一阶段使用独立 `research.db` 和文件产物目录，不直接扩展现有主数据库承载全部研究数据。

**理由：**

- 降低对 `src/storage.py` 的侵入和上游合并冲突。
- 研究数据可以独立备份、迁移和扩容。
- 当前规模无需立即部署 PostgreSQL。

**接受的取舍：** 无跨库外键和事务，通过规范化 ID 与应用服务保证一致性。

**重新评估条件：** 多用户、多 Worker、持续写锁、复杂并行查询或单机容量不足。

### ADR-003：使用 MCP 作为 PEI 数据边界

**状态：** Proposed

**上下文：** PEI 是插件工作流，不是 DSA 可导入 SDK；研究数据必须保持来源和权限边界。

**决策：** PEI 通过只读、领域级 MCP 工具获取数据，不直接访问数据库和文件系统。

**理由：**

- MCP 是 Codex 官方支持的外部工具和上下文边界。
- 工具级 Schema、权限和审计比 Prompt 拼接稳定。
- MCP 可在不改 PEI 工作流的情况下替换底层数据源。

**接受的取舍：** 需要维护 MCP Schema 和兼容版本。

**重新评估条件：** OpenAI 提供稳定 PEI API，或插件运行面不再支持所需 MCP 能力。

### ADR-004：深度任务使用持久化队列

**状态：** Proposed

**上下文：** 现有 DSA TaskQueue 以进程内线程池和内存状态为主，不能保证长任务跨重启恢复。

**决策：** 使用 `research_job`、租约和 heartbeat 构成数据库持久化队列，暂不引入 Celery/Kafka。

**理由：**

- 满足当前单 Worker 和低并发需求。
- 保留可恢复、幂等、重试和审计能力。
- 可以在任务量被证明足够大时再替换队列实现。

**接受的取舍：** Worker 采用轮询或长轮询，实时性低于专用消息队列。

**重新评估条件：** 任务吞吐、并发和延迟要求持续超过数据库队列能力。

### ADR-005：模型只读，Worker 校验后写回

**状态：** Proposed

**上下文：** 让 LLM 直接写数据库会扩大误操作、Prompt Injection 和 Schema 漂移风险。

**决策：** 自动 PEI MCP 只读；Worker 使用 JSON Schema 与 Evidence ID 校验输出后，通过受控 API 写入。

**理由：**

- 清晰分离推理和持久化职责。
- 可以保留失败产物而不污染正式报告。
- 写入权限、幂等和审计由确定性代码执行。

**接受的取舍：** Worker 需要额外的校验与映射代码。

**重新评估条件：** 暂无需要放宽的合理条件；即使未来开放写工具，也应保持人工审批和细粒度权限。

## 23. 分阶段实施

### Phase 0：可行性垂直切片

预计 1–3 天，只验证一只股票和一个工作流：

1. 安装并启用 PEI。
2. 创建返回 fixture 的最小只读 MCP。
3. 使用 `codex exec` 调用 PEI。
4. 生成符合 JSON Schema 的报告。
5. 验证插件、MCP、超时、产物和错误链路。

本阶段不修改核心数据模型和 Web UI。

### Phase 1：研究数据基础

预计 1–2 周：

- Research DB 和迁移。
- Tushare Research Provider。
- Official Disclosure Provider。
- Point-in-time 财务事实。
- 复权口径统一。
- Evidence Pack。
- 数据质量门禁。
- 数据源契约测试。

### Phase 2：交互式 PEI

预计约 1 周：

- DSA Research MCP。
- 在 Codex 中人工触发 PEI。
- 报告保存和来源引用。
- 报告版本。
- Research Center 基础页面。

先验证“Codex 主动调用 DSA”，再实现“DSA 无人值守调用 Codex”。

### Phase 3：自动 Worker

预计 1–2 周：

- 持久化任务和 Worker API。
- `codex exec` Worker。
- 租约、heartbeat、取消和重试。
- JSON Schema 校验。
- Token、耗时和错误记录。
- SSE 进度和人工审核。

### Phase 4：事件驱动

预计约 1 周：

- 财报公告自动触发。
- 重大事项触发 Thesis Update。
- Catalyst Calendar。
- DSA 技术信号触发研究候选。
- 通知、冷却和幂等去重。

单人开发粗略估计：4–7 周形成可用版本，之后持续增强公告解析、财务模型和数据覆盖。

## 24. 第一版端到端验收

建议以 `600519` 的最新正式财报作为第一条垂直切片：

> 在 DSA 中选择 `600519`，创建“财报深挖”任务；系统归档最新官方财报，生成 point-in-time Evidence Pack，调用 PEI Earnings Deep Dive，返回带引用的结构化报告，并在 Research Center 展示。

必须满足：

- 任务重启后可恢复。
- 重复提交不会产生重复研究。
- 报告显示 `as_of` 和 data cutoff。
- 核心财务数字有 Evidence ID 和来源。
- 缺少关键财报时任务进入 `blocked_data`。
- 前复权和不复权不会静默混用。
- PEI、MCP 或数据源不可用时有明确错误。
- Schema 校验失败的报告不会发布。
- 关闭研究功能后不影响 DSA 原有分析。
- 同一 Evidence Pack 可以重新执行并复现输入边界。

## 25. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 官方公告接口或网页变化 | 抓取失败、公告缺失 | Provider 隔离、fixture 测试、缓存、明确降级 |
| Tushare 权限或限流 | 财务数据不完整 | 能力探测、限流、缓存、质量门禁、官方公告兜底 |
| PEI 插件升级导致输出变化 | Schema 校验失败 | 记录版本、升级前回归、失败不发布 |
| 公告 Prompt Injection | 模型越权或错误工具调用 | 内容不可信标记、只读 MCP、域名白名单 |
| SQLite 写锁 | 任务和数据写入延迟 | 单写入服务、WAL、批量写、迁移 PostgreSQL 触发点 |
| Codex 调用成本失控 | Token 和时间超预算 | 幂等、并发 1、工作流预算、人工触发高成本任务 |
| DSA 上游快速更新 | Fork 合并冲突 | 新增模块、稳定 API 边界、避免大改核心文件 |
| 数据口径错误 | 估值和结论错误 | PIT 测试、单位/币种/复权测试、证据链和人工审核 |

## 26. 回滚策略

所有阶段必须保持 opt-in：

1. Research 功能默认关闭时，DSA 原有分析、调度、通知和 Web 页面不受影响。
2. MCP 或 Worker 故障时，只停止新 PEI 任务，不影响普通 DSA 分析。
3. 关闭 Worker 后，已排队任务保留为 `queued` 或可取消状态。
4. Research DB 与 DSA Core DB 分离，可独立备份、恢复或移除。
5. 新 API 通过独立 router 注册，可整体撤销而不改变原有 `/analysis` 和 `/agent` 契约。
6. 前端 Research Center 使用独立路由，关闭功能后隐藏入口。
7. PEI 输出 Schema 升级采用新版本并保留旧报告读取兼容，不原地重写历史报告。

## 27. 实施前待确认事项

开始 Phase 0 前需要确认：

1. DSA 主要以原生 Python、Docker 还是桌面端运行。
2. PEI Worker 是否与 DSA 在同一台机器。
3. 首期股票池数量和每日预期研究任务数。
4. Tushare Pro 当前可用权限和积分等级。
5. 第一阶段是否只做 A 股。
6. 是否接受“先在 Codex 手动触发，验证后再做 DSA 一键自动触发”的实施顺序。
7. 报告是否需要生成 Excel/DCF 文件，还是先只输出 JSON 和 Markdown。

默认推荐：DSA 可继续 Docker 或原生运行，PEI Worker 在安装 Codex 的宿主机运行；第一阶段只做 A 股、JSON 和 Markdown，并从交互式 PEI 垂直切片开始。
