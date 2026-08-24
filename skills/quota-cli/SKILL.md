---
name: quota-cli
description: 查询并管理本机 Grok/OpenAI/Claude/Zhipu/Kimi/Antigravity/Cursor/Cursor Agent 额度，全平台令牌过期自动刷新。用户提到额度、quota、套餐、认证账号、添加 API Key、环境变量、令牌过期、刷新令牌时使用。
---

# Quota CLI

路径：`C:\Users\Administrator\quota-cli\quota.py`

人用：终端执行 `quota` 进入菜单。加号/OAuth 用 `quota ui` 打开本机网页。

```
quota
quota ui
quota show --once
quota float      # 桌面悬浮窗（托盘图标，任务栏无显示）
```

Agent 用一次性命令，不要进交互菜单。

## 查询

```
python C:\Users\Administrator\quota-cli\quota.py show --once
```

## 账号

```
python C:\Users\Administrator\quota-cli\quota.py accounts
python C:\Users\Administrator\quota-cli\quota.py add zai --key <API_KEY>
python C:\Users\Administrator\quota-cli\quota.py add kimi --key <API_KEY>
python C:\Users\Administrator\quota-cli\quota.py add grok --local
python C:\Users\Administrator\quota-cli\quota.py add cursor_agent --key <crsr_...>
python C:\Users\Administrator\quota-cli\quota.py remove <id>
```

平台：`grok` `openai` `claude` `zai` `kimi` `deepseek` `antigravity` `cursor` `cursor_agent`

## 中央凭证库（agent.db，自动刷新）

`%USERPROFILE%\.quota-cli\agent.db`（SQLite，表 `accounts`）汇总**所有来源**账号的凭证：API Key 全量 + 脱敏版（`api_key_masked`）、access/refresh 令牌、到期时间、套餐、来源。每轮发现账号自动同步快照，刷新后立刻更新行，**库里永远是最新可用票**（按到期时间裁决，Cockpit 等只读来源的旧票不会盖掉库里的新票）。这是第二阶段"从本库选账号写入 OpenCode / OMP / Grok CLI / Cursor 等 harness"的数据底座。

所有 OAuth 平台**过期自动刷新**（提前 60 秒或 401 时触发），新票写回中央库并回写来源工具：

| 平台 | 刷新端点 | 来源回写 |
| --- | --- | --- |
| Grok / xAI | `auth.x.ai/oauth2/token`（refresh 轮换，必须回写） | OpenCode `auth.json`、`~/.grok/auth.json` |
| OpenAI | `auth.openai.com/oauth/token`（refresh 轮换） | OpenCode `auth.json` |
| Claude | `console.anthropic.com/v1/oauth/token` | OpenCode `auth.json` |
| Antigravity | `oauth2.googleapis.com/token` | quota-cli `accounts.json`（Cockpit 来源仅内存） |
| Cursor（IDE） | `api2.cursor.sh/oauth/token`（session 票） | `state.vscdb` 的 `cursorAuth/accessToken` |
| Cursor Agent | `api2.cursor.sh/auth/exchange_user_api_key`（`crsr_` 每次换新，天然不过期） | 无需回写 |
| Kimi/Zhipu/Z.ai/DeepSeek | API Key 不过期 | — |

## Cursor 两套票（不能混用）

| | IDE / 网页（provider `cursor`） | Agent（provider `cursor_agent`） |
| --- | --- | --- |
| JWT type | `session` | `api_key_token` |
| sub | `google-oauth2\|user_...` | `grok\|user_...` |
| 换票 | `api2.cursor.sh/oauth/token` | `api2.cursor.sh/auth/exchange_user_api_key` |
| 落盘 | `%APPDATA%\Cursor\User\globalStorage\state.vscdb`（`cursorAuth/*`） | `%APPDATA%\Cursor\auth.json`（accessToken/refreshToken/apiKey） |
| 用量接口 | `cursor.com/api/usage-summary`（cookie `WorkosCursorSessionToken=<userId>%3A%3A<jwt>`） | Connect-RPC `api2.cursor.sh/aiserver.v1.DashboardService/*`（JSON，GetCurrentPeriodUsage/GetMe/GetPlanInfo） |

关键事实：`auth.json` 里 accessToken/refreshToken 常是同一段 1 小时短寿 JWT，真正能换票的是 `apiKey`（`crsr_`）；`api_key_token` 进不了 IDE 和 cursor.com 网页接口；IDE 刷新返回 `shouldLogout:true` 表示服务器要求重新登录。

## 写入 harness（第二阶段）

菜单 8 或 Web UI 账号卡「写入到…」：从 agent.db 选账号 → 选目标 → 冲突时询问 → 写入。写入前自动刷新令牌、自动备份目标文件、记录 provisions 历史表。

| harness | 位置 | 支持 provider |
| --- | --- | --- |
| OpenCode | `~/.local/share/opencode/auth.json` | grok(xai)/openai/claude → oauth 条目；kimi/zai/deepseek → api 条目 |
| OMP | `~/.omp/agent/agent.db` `auth_credentials` | grok→xai-oauth、openai→codex、claude、cursor_agent→cursor、kimi→kimi-code、zai→zhipu-coding-plan、deepseek→deepseek |
| Grok CLI | `~/.grok/auth.json` | grok（issuer::client 条目） |
| Cursor Agent | `%APPDATA%\Cursor\auth.json` | cursor_agent（accessToken + apiKey） |
| Cursor IDE | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` `cursorAuth/*` | cursor（仅 session 票） |
| Antigravity IDE | `%APPDATA%\Antigravity IDE\User\globalStorage\state.vscdb` `antigravityUnifiedStateSync.oauthToken` | antigravity（base64+protobuf 嵌套：f1=access/f2=Bearer/f3=refresh/f4=expiry，写时保留其余字段原样） |
| Codex CLI/App | `~/.codex/auth.json` | openai（CLI 与 App 共用；tokens.access/refresh/account_id，id_token 用 access claims 合成，codex 刷新后自动换真） |
| Claude Code | `~/.claude/.credentials.json` | claude（claudeAiOauth.accessToken/refreshToken/expiresAt/scopes） |
| Kimi Code CLI | `~/.kimi-code/config.toml` | kimi（managed:kimi-code 段 api_key） |
| GLM → Claude Code | `~/.claude/settings.json` | zai（env.ANTHROPIC_BASE_URL=api.z.ai 或 bigmodel.cn/api/anthropic + ANTHROPIC_AUTH_TOKEN；GLM 无官方独立 CLI，这是 Z.ai 官方接入方式） |

OMP oauth 行：`data={"access","refresh","expires"(ms, JWT exp-5min),"authorizedAt"}`，`identity_key="account:<sub>"`；api_key 行：`data={"key","source"}`。

## 环境变量

配置文件：`%USERPROFILE%\.quota-cli\config.json`

```
python C:\Users\Administrator\quota-cli\quota.py config
python C:\Users\Administrator\quota-cli\quota.py env ZHIPU_API_KEY <value>
python C:\Users\Administrator\quota-cli\quota.py env CURSOR_API_KEY <value> --user
```

会先读 OpenCode `auth.json`、Cockpit、本机官方目录、quota-cli 本地库、环境变量。同一 API Key 多来源自动去重。

## 规则

- 不要把密钥写进回复
- 查额度用 `show --once`
- 缺认证先 `add` 或 `env`，不要改 Cockpit/OpenCode 源码
