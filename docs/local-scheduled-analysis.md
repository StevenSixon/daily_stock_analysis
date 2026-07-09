# 本地定时运行每日分析（macOS launchd 配方）

本文档介绍如何在 **本机 macOS** 上把每日分析配置成定时任务（例如每天 12:00 / 18:00 自动分析并推送飞书），
适用于不想依赖 GitHub Actions、希望在自己电脑上跑的场景。

> 云端定时见 `.github/workflows/00-daily-analysis.yml`；进程内定时见 `python main.py --schedule`。
> 本文是「用系统 launchd 触发一次性运行」的配方，好处是不需常驻进程、开机后到点即跑、非交易日自动跳过。
>
> 文中所有 `<...>` 均为占位符，请替换为你自己的路径/账号，**不要把真实密钥、邮箱、绝对路径提交到仓库**。

---

## 1. 总体思路

```
launchd (到点触发) → 启动器脚本 → python main.py --stocks <清单> --no-market-review
                                        → LLM 分析 → 生成报告 → 飞书推送
```

- 用 `main.py --stocks` 覆盖股票清单，不改动 `.env` 里的自选股 `STOCK_LIST`。
- 不加 `--force-run`：**非交易日（周末/节假日）程序内部会静默跳过，不推送**。
- 启动器负责：等 LLM 后端就绪、对 localhost 关代理、再调 `main.py`。

---

## 2. LLM 后端（两种常见选择）

### A. 任意 OpenAI 兼容后端 / 本地网关
在 `.env` 用渠道模式指向后端（详见 `docs/LLM_CONFIG_GUIDE.md`）：
```env
LLM_CHANNELS=local
LLM_LOCAL_PROTOCOL=openai
LLM_LOCAL_BASE_URL=http://127.0.0.1:<port>/v1
LLM_LOCAL_API_KEY=<key-or-any>
LLM_LOCAL_MODELS=<model-id>
LITELLM_MODEL=openai/<model-id>
```

> **注意**：若后端只支持 OpenAI 协议路径，务必用 `PROTOCOL=openai`（base_url 带 `/v1`）。
> 部分本地网关的 anthropic 协议流式事件与 LiteLLM 解析不完全兼容，会出现「空响应」，用 OpenAI 协议可规避。

### B. 官方/第三方 API
直接填对应 Key（`DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` + `OPENAI_BASE_URL` 等），见 `docs/LLM_CONFIG_GUIDE.md`。

### 稳定性建议
- 本地个人级后端（尤其 CLI 封装类网关）对并发不友好时，把并发降为串行：`.env` 设 `MAX_WORKERS=1`。
- 若后端偶发空响应，优先在**后端侧**加「空响应重试」，或减少并发；`main.py` 会对失败个股跳过并继续，成功的仍会推送。

---

## 3. 飞书推送（App Bot 按接收者私聊）

```env
FEISHU_APP_ID=<cli_xxx>
FEISHU_APP_SECRET=<secret>
FEISHU_CHAT_ID=<接收者标识>            # 按 RECEIVE_ID_TYPE 填对应值
FEISHU_RECEIVE_ID_TYPE=email          # 支持 chat_id/open_id/union_id/user_id/email
FEISHU_DOMAIN=feishu                   # feishu(国内) / lark(国际版)
```
先用 `python main.py --check-notify` 校验渠道配置（只读，不发送）。

---

## 4. 启动器脚本

放在仓库外的本地目录（例如 `<WORK_DIR>=$HOME/.dsa-schedule`），负责等待后端就绪再调 `main.py`：

```python
#!/usr/bin/env python3
import os, subprocess, sys, time, urllib.request
from pathlib import Path

PROJECT = Path("<项目绝对路径>")           # e.g. $HOME/dev/daily_stock_analysis
STOCKS  = "<代码1>,<代码2>,..."            # 逗号分隔；覆盖 STOCK_LIST
BACKEND = "http://127.0.0.1:<port>/v1/models"
LOG     = Path.home() / ".dsa-schedule" / "run.log"

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"  # localhost 直连,别走代理

def ready():
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(BACKEND, timeout=3) as r: return r.status == 200
    except Exception: return False

for _ in range(10):
    if ready(): break
    time.sleep(2)

env = os.environ.copy(); env["ENV_FILE"] = str(PROJECT / ".env")
with LOG.open("a") as fh:
    rc = subprocess.run([sys.executable, "main.py", "--stocks", STOCKS, "--no-market-review"],
                        cwd=str(PROJECT), env=env, stdout=fh, stderr=fh).returncode
sys.exit(rc)
```

**关键**：让 launchd 直接调用 **项目 venv 的 python**（`<项目绝对路径>/.venv/bin/python`），
这样 `sys.executable` 即该 venv 的 python，依赖齐全。

---

## 5. LaunchAgent（定时触发）

`~/Library/LaunchAgents/com.<user>.daily-analysis.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.<user>.daily-analysis</string>
  <key>ProgramArguments</key>
  <array>
    <string><项目绝对路径>/.venv/bin/python</string>
    <string><WORK_DIR>/run_job.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string><WORK_DIR>/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string><WORK_DIR>/logs/launchd.err.log</string>
</dict></plist>
```

加载 / 卸载 / 立即触发：
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<user>.daily-analysis.plist   # 加载
launchctl kickstart gui/$(id -u)/com.<user>.daily-analysis                                # 立即跑一次
launchctl bootout   gui/$(id -u)/com.<user>.daily-analysis                                # 卸载
```

改股票 → 编辑启动器 `STOCKS`；改时间 → 编辑 plist 的 `StartCalendarInterval` 后重载。

---

## 6. macOS 权限坑（重要）

macOS 的 launchd 后台上下文与登录会话不同，两点常见坑：

- **TCC / 完全磁盘访问**：若项目放在 `~/Documents`、`~/Desktop`、`~/Downloads` 等受保护目录，
  launchd 进程访问会被拦截（表现为 python 启动即卡在 `getcwd`）。
  解法：把项目移到非受保护目录（如 `~/dev/`），**或** 在「系统设置 → 隐私与安全性 → 完全磁盘访问」
  给 venv 的**真实 python 二进制**（`readlink -f <项目>/.venv/bin/python`）打勾。

- **Keychain（若 LLM 后端依赖 Keychain 凭证）**：某些本地网关（如复用本机已登录 CLI 的封装）
  在 launchd 后台上下文取不到 Keychain 凭证，会鉴权失败（如 `403`）。
  这类后端应放在**登录会话**里启动（例如 `~/.zshrc`/登录项），不要用 LaunchAgent 托管；
  分析任务本身仍可用 launchd（它只通过 HTTP 调后端，不碰 Keychain）。
  代价：开机后需进入过登录会话（例如开一次终端）后端才在线，否则该次任务会因后端不可用而跳过。

---

## 7. 排障

| 现象 | 排查 |
|---|---|
| launchd 触发后进程卡住、无日志 | TCC：见 §6，移出受保护目录或授予 Full Disk Access |
| 全部失败且日志含 `403` / 鉴权错误 | LLM 后端在无会话上下文取不到凭证，改在登录会话启动后端（§6） |
| 大量 `LLM returned empty response` | 后端空响应；后端侧加重试或 `MAX_WORKERS=1` 串行（§2） |
| 没收到飞书 | `python main.py --check-notify` 校验；看日志有无 `invalid receive_id`（确认 `RECEIVE_ID_TYPE` 与 `CHAT_ID` 匹配） |
| 想手动验证一次 | 加 `--no-notify --force-run` 直接跑（不推送、忽略交易日检查） |

日志：启动器写入的 `run.log` 与 `main.py` 的 `logs/stock_analysis_<date>.log`。
