---
name: dushan-quota
description: 查询并管理本机 Grok/OpenAI/Claude/Zhipu/Kimi/Antigravity/Cursor/Cursor Agent 额度；全平台令牌过期自动刷新并汇总进 agent.db；可写入 OpenCode/OMP/各官方 CLI；Web UI 支持重置 OpenAI 额度、隐藏卡片、主题切换。用户提到额度、quota、套餐、认证账号、添加 API Key、环境变量、令牌过期、刷新令牌、写入凭证、重置额度时使用。
---

# Dushan Quota

路径：`C:\Users\Administrator\dushan-quota\quota.py`

人用：终端执行 `quota` 直接启动悬浮窗（内嵌 Web 服务，关闭悬浮窗即全部停止）。点悬浮窗标题栏 🌐 打开 Web 配置页，加号/OAuth 都在网页里完成。

```
quota            # 启动悬浮窗（脱离终端，关终端不影响）
quota ui         # 打开 Web 配置页（服务未运行时会先拉起悬浮窗）
quota float      # 同 quota
quota ui-run     # headless 服务器专用：不依赖显示器，前台单独跑 Web 服务
```

悬浮窗：托盘图标，任务栏无显示，可拖动/缩放/置顶/调透明度/自定义背景图，设置面板可切换 5 款配色主题。

终端没有查询命令；Agent 一律走本机 Web API（先 `quota ui` 确保服务已启动），不要进交互流程。

## 查询

```
curl http://127.0.0.1:18765/api/quota
curl "http://127.0.0.1:18765/api/quota?force=1"  # 仅在用户明确要求立即联网时使用
```

## 账号

```
python C:\Users\Administrator\dushan-quota\quota.py accounts
python C:\Users\Administrator\dushan-quota\quota.py add zai --key <API_KEY>
python C:\Users\Administrator\dushan-quota\quota.py add kimi --key <API_KEY>
python C:\Users\Administrator\dushan-quota\quota.py add grok --local
python C:\Users\Administrator\dushan-quota\quota.py add cursor_agent --key <crsr_...>
python C:\Users\Administrator\dushan-quota\quota.py remove <id>
```

平台：`grok` `openai` `claude` `zai` `kimi` `deepseek` `antigravity` `cursor` `cursor_agent`

## Web UI（http://127.0.0.1:18765）

界面能力：响应式卡片、左侧平台筛选、右侧「关注」面板（用量最低榜/即将重置榜，100% 不进榜）、四款主题（暗黑鎏金/蓝天白云/粉红少女/绿意盎然）、卡片 ✕（本地账号删除、其他隐藏可恢复）、订阅起止时间展示（xAI、Cursor Agent 已接入，记录进 agent.db）、运行日志面板。

Agent 可用的 HTTP API（先 `quota ui` 启动）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/quota` | 读取跨进程共享额度快照（results + hidden_count + snapshot）；用户明确要求立即联网时才用 `/api/quota?force=1` |
| POST | `/api/reset` | 消费一次 OpenAI reset credit；必须由用户明确确认，body `{"provider":"openai","identity":"<identity>","confirmed":true}` |
| GET | `/api/provision/targets?provider=<p>` | 查某平台可写入的 harness |
| POST | `/api/provision` | 写入 harness，body `{"provider","identity","harness","confirmed"}`；返回 `needs_confirm` 时用 `confirmed:true` 重发 |
| POST | `/api/hide` / `/api/unhide` | 隐藏/恢复卡片，body `{"provider","identity"}`；unhide 空 body 全恢复 |
| GET/POST | `/api/config` | 读/写设置（`watch_seconds` 自动刷新间隔秒数，0=仅手动） |
| GET | `/api/logs` | 运行日志（内存环形缓冲 500 条，`?limit=` `?level=` 过滤；同时落盘 `quota.log` JSONL） |
| POST | `/api/float` | 启动桌面悬浮窗 |
| GET | `/api/rules` | 平台与添加方式 |
| POST | `/api/accounts/key` | 添加 API Key，body `{"provider","key"}` |

## 中央凭证库（agent.db，自动刷新）

`%USERPROFILE%\.quota-cli\agent.db`（SQLite，表 `accounts`）汇总**所有来源**账号的凭证：API Key 全量 + 脱敏版（`api_key_masked`）、access/refresh 令牌、到期时间、套餐、来源、订阅起止（`plan_start/plan_end`）。每轮发现账号自动同步快照，刷新后立刻更新行，**库里永远是最新可用票**（按到期时间裁决，Cockpit 等只读来源的旧票不会盖掉库里的新票）。这是"从本库选账号写入各 harness"的数据底座。

所有 OAuth 平台**过期自动刷新**（提前 60 秒或 401 时触发），新票写回中央库并回写来源工具：

| 平台 | 刷新端点 | 来源回写 |
| --- | --- | --- |
| Grok / xAI | `auth.x.ai/oauth2/token`（refresh 轮换，必须回写） | OpenCode `auth.json`、`~/.grok/auth.json` |
| OpenAI | `auth.openai.com/oauth/token`（refresh 轮换） | OpenCode `auth.json` |
| Claude | `console.anthropic.com/v1/oauth/token` | OpenCode `auth.json` |
| Antigravity | `oauth2.googleapis.com/token` | dushan-quota `accounts.json`（Cockpit 来源仅内存） |
| Cursor（IDE） | `api2.cursor.sh/oauth/token`（session 票） | `state.vscdb` 的 `cursorAuth/accessToken` |
| Cursor Agent | `api2.cursor.sh/auth/exchange_user_api_key`（`crsr_` 每次换新，天然不过期） | 无需回写 |
| Kimi/Zhipu/Z.ai/DeepSeek | API Key 不过期 | — |

OpenAI 重置次数要区分两个字段：`available_count` 是总剩余，`applicable_available_count` 是当前可使用数。只有二者都大于 0、HTTP API 收到 `confirmed:true` 后，后端才允许调用消费端点；状态缺失或当前可用为 0 时一律 fail closed，不调用消费接口。

Web UI 与悬浮窗共用 `%USERPROFILE%\.quota-cli\quota-snapshot.json`。跨进程锁把同一周期的并发请求合并为一次联网刷新，界面手动刷新或 API 的 `?force=1` 才绕过正常缓存周期；快照不写入任何认证密钥。

## Cursor 两套票（不能混用）

| | IDE / 网页（provider `cursor`） | Agent（provider `cursor_agent`） |
| --- | --- | --- |
| JWT type | `session` | `api_key_token` |
| sub | `google-oauth2\|user_...` | `grok\|user_...` |
| 换票 | `api2.cursor.sh/oauth/token` | `api2.cursor.sh/auth/exchange_user_api_key` |
| 落盘 | `%APPDATA%\Cursor\User\globalStorage\state.vscdb`（`cursorAuth/*`） | `%APPDATA%\Cursor\auth.json`（accessToken/refreshToken/apiKey） |
| 用量接口 | `cursor.com/api/usage-summary`（cookie `WorkosCursorSessionToken=<userId>%3A%3A<jwt>`） | Connect-RPC `api2.cursor.sh/aiserver.v1.DashboardService/*`（JSON，GetCurrentPeriodUsage/GetMe/GetPlanInfo） |

关键事实：`auth.json` 里 accessToken/refreshToken 常是同一段 1 小时短寿 JWT，真正能换票的是 `apiKey`（`crsr_`）；`api_key_token` 进不了 IDE 和 cursor.com 网页接口；IDE 刷新返回 `shouldLogout:true` 表示服务器要求重新登录。

## 写入 harness

Web UI 账号卡「写入到…」或 `/api/provision`：从 agent.db 选账号 → 选目标 → 冲突时询问 → 写入。写入前自动刷新令牌、自动备份目标文件、记录 provisions 历史表。

| harness | 位置 | 支持 provider |
| --- | --- | --- |
| OpenCode | `~/.local/share/opencode/auth.json` | grok(xai)/openai/claude → oauth 条目；kimi/zai/deepseek → api 条目 |
| OMP | `~/.omp/agent/agent.db` `auth_credentials` | grok→xai-oauth、OpenAI API Key→openai、OpenAI Codex OAuth→openai-codex、claude、cursor_agent→cursor、kimi→kimi-code、zai→zhipu-coding-plan、deepseek→deepseek |
| Grok CLI | `~/.grok/auth.json` | grok（issuer::client 条目） |
| Cursor Agent | `%APPDATA%\Cursor\auth.json` | cursor_agent（accessToken + apiKey） |
| Cursor IDE | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` `cursorAuth/*` | cursor（仅 session 票） |
| Antigravity IDE | `%APPDATA%\Antigravity IDE\User\globalStorage\state.vscdb` `antigravityUnifiedStateSync.oauthToken` | antigravity（base64+protobuf 嵌套：f1=access/f2=Bearer/f3=refresh/f4=expiry，写时保留其余字段原样） |
| Codex CLI/App | `~/.codex/auth.json` | openai（CLI 与 App 共用；tokens.access/refresh/account_id，id_token 用 access claims 合成，codex 刷新后自动换真） |
| Claude Code | `~/.claude/.credentials.json` | claude（claudeAiOauth.accessToken/refreshToken/expiresAt/scopes） |
| Kimi Code CLI | `~/.kimi-code/config.toml` | kimi（managed:kimi-code 段 api_key） |
| GLM → Claude Code | `~/.claude/settings.json` | zai（env.ANTHROPIC_BASE_URL=api.z.ai 或 bigmodel.cn/api/anthropic + ANTHROPIC_AUTH_TOKEN；GLM 无官方独立 CLI，这是 Z.ai 官方接入方式） |

OMP oauth 行：`data={"access","refresh","expires"(ms, JWT exp-5min),"authorizedAt"}`；OpenAI Codex 额外写 `accountId/email/orgId/orgName`，provider 必须是 `openai-codex`，identity 为 `email:<email>|org:<workspace>`（缺 email 时回退 account）；api_key 行：`data={"key","source"}`。cursor_agent 的 refresh 必须填 `crsr_`，不能填短寿 JWT。

注意（已内置看护）：OMP 的 `refreshCursorToken` 会把 `exchange_user_api_key` 返回的短寿 JWT 覆盖进 refresh，第二轮刷新必 403 并把凭证写进 `auth_credential_blocks` 拉黑。dushan-quota 每轮拉取时自动修复（`lib/provision.py guard_omp_cursor`）：refresh 不是 crsr_ / 已过期 / 有 block → 换新票并清除拉黑，仅对已写入过 OMP 的账号生效。

## 环境变量

配置文件：`%USERPROFILE%\.quota-cli\config.json`

```
python C:\Users\Administrator\dushan-quota\quota.py config
python C:\Users\Administrator\dushan-quota\quota.py env ZHIPU_API_KEY <value>
python C:\Users\Administrator\dushan-quota\quota.py env CURSOR_API_KEY <value> --user
```

会先读 OpenCode `auth.json`、Cockpit、本机官方目录、dushan-quota 本地库、环境变量。同一 API Key 多来源自动去重。

## 规则

- 不要把密钥写进回复
- 查额度用 `/api/quota`（先 `quota ui` 启动）
- 不要替用户调用 `/api/reset`；只有用户明确要求消费并再次确认时才可提交 `confirmed:true`
- 缺认证先 `add` 或 `env`，不要改 Cockpit/OpenCode 源码
