# PEI Research Center 使用与运维

本文描述 DSA + Public Equity Investing 深度研究能力的实际运行契约。系统默认关闭；启用后仍使用独立 Research DB、独立 Codex Home 和独立 Worker，不改变原有股票分析、调度、告警和通知主流程。完整架构决策见 [系统设计](architecture/dsa-pei-research-system-design.md)，合成纵切见 [Phase 0 指南](pei-research-phase0.md)。

## 已实现范围

- A 股证券主数据、Tushare 三表/指标/分红/行情与复权口径采集。
- 巨潮官方公告发现、HTTPS 域名白名单、大小限制、原文归档与 PDF 文本降级提取。
- 独立 SQLite Research DB、版本标记、WAL、外键、PIT 查询与 append-only 修订记录。
- 不可变 Evidence Pack、`as_of`/data cutoff、质量门禁、来源清单和内容哈希。
- `/api/v1/research` 任务、报告、审核、时间线和文档 API。
- 独立 scoped Bearer Worker API；网页登录 Cookie 不参与 Worker 认证。
- 只读 STDIO MCP、明确工具 allowlist、受控 `codex exec`、租约、heartbeat、取消、重试和服务端二次校验。
- Web Research Center：任务创建/取消、数据同步、队列状态、报告、Evidence ID 和人工审核。
- 可选公告/告警事件触发、报告发布通知、论点和催化剂结构化落库。
- 单次/月度 Token 预算、月度用量状态，以及定时模式的公告增量扫描。

首版不包含 Excel/DCF 文件生成、SSE 实时流、非 A 股数据源、无人审核自动发布或多 Worker 并发扩容。页面采用 10 秒轮询显示活跃任务状态。

## 启用前准备

1. 安装后端和 Web 依赖：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd apps/dsa-web && npm ci && cd ../..
```

2. 配置可用的 `TUSHARE_TOKEN`。不同 Tushare 权限会导致部分端点降级；质量门禁决定任务进入 `data_ready`、`degraded` 或 `blocked_data`，不会静默编造缺失数据。
3. 创建专用 Codex Home，禁止复用个人 `~/.codex`：

```bash
mkdir -p "$HOME/.codex-dsa-pei"
cp docs/examples/pei-research-codex-config.toml "$HOME/.codex-dsa-pei/config.toml"
```

把模板中的 `<ABSOLUTE_PROJECT_PATH>` 替换为仓库绝对路径，并在该 Codex Home 中安装/确认 Public Equity Investing 插件。插件 skill 和版本以实际落盘 manifest 为准。
4. 生成至少 32 字符的随机 Worker Token，例如：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

仅把 Token 写入本机 `.env` 或秘密管理系统，不写入 Codex config、文档或版本库。

## 最小配置

```dotenv
RESEARCH_ENABLED=true
TUSHARE_TOKEN=<已有 Tushare Token>
RESEARCH_DATABASE_PATH=./data/research/research.db
RESEARCH_EVIDENCE_PACKS_DIR=./data/research/evidence-packs
RESEARCH_DOCUMENTS_DIR=./data/research/documents
RESEARCH_ARTIFACTS_DIR=./data/research/artifacts

RESEARCH_CODEX_HOME=~/.codex-dsa-pei
RESEARCH_PEI_PLUGIN_SKILL=public-equity-investing
RESEARCH_PEI_PLUGIN_VERSION=<已安装版本>
RESEARCH_PEI_WORKFLOW_VERSION=research-v1
RESEARCH_MCP_SERVER_NAME=dsa_research
RESEARCH_MCP_SERVER_VERSION=research-v1

RESEARCH_WORKER_API_URL=http://127.0.0.1:8000/api/v1/research/worker
RESEARCH_WORKER_TOKEN=<随机 Token>
RESEARCH_CODEX_FORWARD_ENV=RESEARCH_WORKER_API_URL,RESEARCH_WORKER_TOKEN
RESEARCH_MCP_ALLOWED_TOOLS=resolve_security,get_evidence_pack_manifest,get_company_profile,get_financial_statements,get_market_history,get_corporate_actions,search_official_filings,get_filing_excerpt,get_previous_research

# 0 表示关闭预算门禁
RESEARCH_RUN_TOKEN_BUDGET=0
RESEARCH_MONTHLY_TOKEN_BUDGET=0
```

Worker 和 DSA 不在同一主机时，`RESEARCH_WORKER_API_URL` 必须使用有效证书的 HTTPS。MCP/Worker 客户端拒绝远程明文 HTTP、URL 内凭据、重定向和环境代理，避免 Bearer Token 被转发到非预期目标。

## 启动与垂直切片

先启动 DSA API：

```bash
.venv/bin/python main.py --serve-only
```

在另一个终端启动单消费者 Worker：

```bash
.venv/bin/python scripts/research_worker.py --verbose
```

`--once` 最多领取一个任务；无任务时退出码为 3，适合健康检查或外部调度。

Web 打开 `/research`，按以下顺序验证 `600519`：

1. “同步研究数据”，归档结构化数据和最新公告。
2. 创建 `earnings_deep_dive`。
3. 确认任务从 `collecting_data` / `data_ready` 进入 `analyzing`、`validating` 和 `awaiting_review`。
4. 打开报告，检查 `as_of`、Evidence ID、引用、Markdown 和报告 hash。
5. 人工选择批准、拒绝或请求修改；只有批准才进入 `published`。

也可调用：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/research/securities/600519/refresh \
  -H 'Content-Type: application/json' \
  -d '{"years":5,"price_basis":"raw","include_disclosures":true}'

curl -X POST http://127.0.0.1:8000/api/v1/research/jobs \
  -H 'Content-Type: application/json' \
  -d '{"security_code":"600519","workflow":"earnings_deep_dive","price_basis":"raw"}'
```

若启用了 `ADMIN_AUTH_ENABLED`，上述用户 API 仍需有效登录 Cookie；Worker API 始终只接受独立 Bearer Token。

## 事件驱动与通知

以下配置默认全部关闭：

```dotenv
RESEARCH_AUTO_TRIGGER_DISCLOSURES=false
RESEARCH_AUTO_TRIGGER_ALERTS=false
RESEARCH_NOTIFY_ON_PUBLISH=false
RESEARCH_DISCLOSURE_SCAN_INTERVAL_MINUTES=360
RESEARCH_DISCLOSURE_SCAN_LOOKBACK_DAYS=45
RESEARCH_DISCLOSURE_MAX_PAGES=5
```

- 公告自动触发只处理新入库公告：定期报告映射 `earnings_deep_dive`，重大事项关键词映射 `thesis_update`。在 `--schedule` 模式下会注册独立扫描任务，动态读取最新 `STOCK_LIST`；单股失败不会中断其他标的或主调度。
- 扫描先从巨潮官方股票清单解析证券 `orgId`，避免仅传股票代码时接口返回 200 但公告为空。已归档的 `source_name + external_id` 不会重复下载；曾发现但归档失败的记录会在后续扫描中重试补齐文件。每轮按时间窗口分页，页数有上限。
- 技术告警只在新 trigger 成功落库时映射低优先级 `long_short_pitch` 候选。
- 幂等键包含证券、事件、工作流和版本；重复扫描返回已有任务。
- 通知仅在人工批准报告后发送，复用 DSA `report` 路由；通知异常不会回滚已发布报告。

## 状态、失败与恢复

任务主要状态：

```text
queued → collecting_data → data_ready → analyzing → validating → awaiting_review → published
                         ↘ blocked_data
analyzing/validating → failed_retryable → analyzing
                    ↘ failed_permanent / cancelled
```

- Worker lease 到期后任务变为 `failed_retryable`；单消费者重启后可重新领取。
- 取消运行中任务是协作式的：Worker 在 heartbeat 收到取消标记，当前 Codex 子进程结束后丢弃结果并落为 `cancelled`。
- Schema、工作流、`as_of` 或 Evidence ID 校验失败时不会创建报告。
- 单次 Token 超限时服务端把本次运行标为永久失败且不创建报告；月度额度耗尽时保留排队任务并暂停领取，跨月后自动恢复。预算为 `0` 时不启用门禁；Codex 无硬截断参数，因此单次额度同时作为提示约束和写回前的强制发布门禁。
- 官方公告解析失败保留原始文件和降级 warning；关键数据不足时 Evidence Pack 为 `blocked_data`。
- 原始价、前复权和后复权分别保存并在 Pack 中明确标注，任务内不静默混用。

## 验证

确定性检查：

```bash
.venv/bin/python -m pytest -q \
  tests/test_pei_output_validator.py \
  tests/test_pei_fixture_mcp_server.py \
  tests/test_pei_runner.py \
  tests/test_research_domain.py \
  tests/test_research_providers.py \
  tests/test_research_api.py \
  tests/test_research_mcp_server.py \
  tests/test_research_triggers.py

./scripts/ci_gate.sh
cd apps/dsa-web && npm run lint && npm run build
```

在线验证还需要有效 Tushare 权限、可访问的官方公告站点、已安装 PEI 插件和可用 Codex 账号；离线测试不会证明这些外部条件。

## 回滚与备份

1. 设置 `RESEARCH_ENABLED=false` 并停止 `scripts/research_worker.py`。Research 导航隐藏，原 DSA 分析/告警/通知继续运行。
2. 保留 `data/research/` 可恢复排队任务和报告；需要彻底移除时，先备份后删除该独立目录。
3. Research router、Worker 和 Web 路由均为独立边界，可回滚相关文件而不修改 `/analysis`、`/agent` 或核心业务数据库。
4. 不删除个人 `~/.codex`；专用 `~/.codex-dsa-pei` 可在停止 Worker 后独立移除。
