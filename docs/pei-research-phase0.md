# DSA + PEI Phase 0 可行性验证

本文说明如何运行设计文档中的 Phase 0 合成数据纵切。当前能力只验证：

- 专用 Codex Home 与已安装 PEI skill 的调用边界。
- 只读、pack-scoped STDIO MCP。
- `codex exec` 的只读 sandbox、超时与 JSONL 审计。
- PEI v1 JSON Schema、`as_of` 和 Evidence ID 服务端二次校验。
- 成功报告与失败原始输出的隔离保存。

本阶段不接入真实 Tushare/公告数据、Research DB、FastAPI、Web、通知或自动 Worker。内置 `600519` 数值和公告均为合成测试数据，不能用于研究或投资决策。

## 前置条件

1. 使用项目支持的 Python 版本创建虚拟环境并安装依赖：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

2. 确认本机 `codex --version` 和 `codex exec --help` 可用。
3. 确认当前账号、Workspace 和角色可以访问 Public Equity Investing 插件。插件可用性可能受套餐、管理员策略和关联 App 权限影响。

## 创建专用 Codex Home

不要复用个人默认的 `~/.codex`。先创建专用目录，并把示例配置复制为 `config.toml`：

```bash
mkdir -p "$HOME/.codex-dsa-pei"
cp docs/examples/pei-phase0-codex-config.toml "$HOME/.codex-dsa-pei/config.toml"
```

编辑新文件，把 `<ABSOLUTE_PROJECT_PATH>` 替换为当前仓库绝对路径。示例只启用以下五个只读工具：

- `resolve_security`
- `get_evidence_pack_manifest`
- `get_financial_statements`
- `get_market_history`
- `get_filing_excerpt`

没有 SQL、Shell、任意 URL、文件写入或数据库写入工具。示例还显式设置 `web_search = "disabled"` 和 `[apps._default] enabled = false`，避免 PEI 声明的第三方 App/Connector 进入 Phase 0 工具面。`required = true` 可确保 MCP 无法启动时 `codex exec` 直接失败。

## 安装并确认 PEI 插件

使用同一个专用 Codex Home 启动 Codex，在 `/plugins` 浏览器中搜索并安装 Public Equity Investing：

```bash
CODEX_HOME="$HOME/.codex-dsa-pei" codex
# 进入 TUI 后输入 /plugins
```

如果已在 ChatGPT 中安装，仍需在该专用 Codex Home 的插件浏览器中确认同步，使插件 bundle 和目标 skill 实际落盘。不同 Codex 版本的 `codex plugin list` 可能只显示本地 Marketplace，不能单独作为远程 PEI 可执行性的证明。

不要根据本文猜测 selector、skill 名称或版本。以插件浏览器和安装后的 manifest 为准；如果其中没有 PEI，应先检查 Workspace 权限或联系管理员。仓库测试通过不能替代真实插件可用性。

## 配置 Phase 0

在项目 `.env` 中加入：

```dotenv
RESEARCH_ENABLED=true
RESEARCH_CODEX_HOME=~/.codex-dsa-pei
RESEARCH_PEI_PLUGIN_SKILL=<插件实际提供的 skill 名称>
RESEARCH_PEI_PLUGIN_VERSION=<已安装插件版本>
RESEARCH_PEI_WORKFLOW_VERSION=earnings-deep-dive-v1
RESEARCH_MCP_SERVER_NAME=dsa_research_fixture
RESEARCH_MCP_SERVER_VERSION=phase0-fixture-v1
RESEARCH_CODEX_TIMEOUT_SECONDS=900
RESEARCH_ARTIFACTS_DIR=./data/research/artifacts
```

模型名不写死。留空 `RESEARCH_CODEX_MODEL` 时使用专用 Codex config 的受控默认值；如需覆盖，再显式设置。

## 运行

先执行不调用模型的本地检查：

```bash
.venv/bin/python scripts/pei_phase0.py preflight
```

该命令验证开关、专用 Codex Home、CLI、完整 Schema、模型传输 Schema、fixture hash、内置参考报告，以及配置版本对应的 PEI skill 是否已物化到插件缓存。它不发起模型调用，因此不能替代一次端到端账号授权验证。

确认后，显式执行一次可能消耗 Codex 用量的合成数据任务：

```bash
.venv/bin/python scripts/pei_phase0.py run
```

Runner 固定使用：

```text
--ephemeral --sandbox read-only --json --strict-config --ignore-rules
--skip-git-repo-check --output-schema ... --output-last-message ...
```

它在独立空目录运行，不把公告内容拼入命令行 Prompt，只传入 `pack_id`、工作流和 `as_of`。子进程只继承最小环境变量集合，并把 `HOME` 绑定到专用 Codex Home，避免个人 `~/.agents/skills` 污染 Worker 的技能上下文；额外变量必须通过 `RESEARCH_CODEX_FORWARD_ENV` 按名称显式允许。

完整 PEI Schema 可能包含 Structured Outputs 传输层不支持的约束。Runner 会从同一 Schema 确定性生成模型可接受的结构子集，模型返回后再用完整 Schema 执行长度、格式、范围、唯一性和 Evidence ID 二次校验；传输层放宽不会放宽发布门禁。

## 产物与失败语义

默认产物位于 `data/research/artifacts/<run_id>/`：

- `raw-output.json`：Codex 最终原始输出，成功与失败都保留。
- `model-output-schema.json`：从完整报告 Schema 生成的 Structured Outputs 传输子集。
- `events.jsonl`：Codex JSONL 事件、MCP 调用和 Token 用量证据。
- `stderr.log`：经过密钥、Token、URL 和本地路径脱敏的诊断信息。
- `validated-report.json`：只有 Schema 和 Evidence ID 校验成功时生成。
- `report.md`：只有验证成功时生成。
- `run-metadata.json`：版本、模型来源、时长、退出码、用量、MCP 工具调用摘要、传输降级、错误分类和产物哈希。

Schema、工作流、`as_of`、Evidence ID 不匹配，或 JSONL 记录出现 Web、Shell、文件修改、协作工具、非 fixture MCP/工具时，任务返回失败，只保留失败证据，不生成正式报告。

## 验证与回滚

离线测试：

```bash
.venv/bin/python -m pytest \
  tests/test_pei_output_validator.py \
  tests/test_pei_fixture_mcp_server.py \
  tests/test_pei_runner.py
```

回滚时将 `RESEARCH_ENABLED=false`。这会阻止 Runner 在创建任务产物前启动，不影响 DSA 原有分析。确认不再需要后，可独立删除专用 Codex Home 和 `data/research/artifacts/`；不要删除个人默认 Codex Home。
