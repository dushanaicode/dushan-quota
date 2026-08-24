# Quota CLI

轻量终端 AI 额度看板。一个 Python 脚本 + 标准库，聚合本机所有 AI 平台账号的剩余额度、套餐、账号信息，终端动态刷新，另带本机 Web UI 完成添加账号和 OAuth 授权。

参考了 [cockpit-tools](https://github.com/jlcodes99/cockpit-tools) 与 [opencode-quota](https://www.npmjs.com/package/@slkiser/opencode-quota) 的认证逻辑，但只保留额度查询这一件事，不做切号、不做多开、不驻留后台。

## 支持平台

| 平台 | 额度内容 | 认证来源 |
|---|---|---|
| Grok / xAI | 周额度、高频/普通任务、套餐 | OpenCode OAuth / 官方 device flow / `~/.grok/auth.json` / Cockpit |
| OpenAI | 5h/周/月窗口、重置次数、套餐 | OpenCode OAuth |
| Claude Code | 额度窗口 | OpenCode OAuth / `~/.claude` |
| Zhipu / Z.ai | 5h/周/月窗口 | OpenCode / Cockpit / API Key / 环境变量 |
| Kimi Code | 周/5h 窗口 | OpenCode / API Key / 环境变量 |
| DeepSeek | 余额（CNY/USD） | OpenCode / API Key / 环境变量 |
| Antigravity | Gemini / Claude+GPT 周/5h 窗口 | Cockpit / Google OAuth |
| Cursor | 用量汇总 | 本机 state.vscdb / 官方登录流 |

悬浮窗（`quota float`）：无边框置顶小窗，半透明背景（20%–100% 可调），可拖动；点击 ⚙ 可按平台勾选显示内容、只看百分比行，设置持久化到 `~/.quota-cli/config.json`。需要 `pywebview`（Windows 用 Edge WebView2）。

认证读取顺序：OpenCode `auth.json` → Cockpit 账号库（需 pycryptodome）→ 本机官方目录 → quota-cli 本地库 → 环境变量。按账号身份去重，支持多账号。

## 安装

要求 Python 3.10+。

```
git clone https://github.com/<owner>/quota-cli.git
cd quota-cli
pip install -r requirements.txt
```

Windows：双击或运行 `install.cmd`（把仓库目录加进 PATH），之后任意终端执行 `quota`。

macOS / Linux：`sh install.sh`，之后执行 `quota`。

不想安装也可以直接：`python quota.py`

## 使用

```
quota            # 交互菜单：查看 / 动态刷新 / 账号管理 / 环境变量 / Web UI / 悬浮窗
quota ui         # 打开本机 Web UI（127.0.0.1），网页里添加账号、走 OAuth
quota float      # 桌面悬浮窗：半透明、可拖动、置顶
quota show       # 动态刷新额度（默认 15s，可在 config 改）
quota show --once  # 查一次就退出（脚本/Agent 用）
quota accounts   # 查看本地账号库
quota add        # 交互式添加账号
quota add zai --key <API_KEY>
quota add zai --env        # 从环境变量导入
quota add grok --local     # 从本机已登录的客户端导入
quota remove <id>
quota config      # 查看配置与环境变量状态
quota env KIMI_API_KEY <value> [--user]
quota rules       # 各平台认证方式
```

Web UI 里选平台 → OAuth 授权：会显示授权链接，复制到你指定的浏览器打开（不强制跳默认浏览器）。支持 Grok（xAI device flow）、Antigravity（Google OAuth，回调到本机）、Cursor（官方登录流轮询）。

Antigravity 的 Google OAuth 需要先配置 client 凭据（不入仓库）：

```
quota env QUOTA_AGY_CLIENT_ID <id>
quota env QUOTA_AGY_CLIENT_SECRET <secret>
```

取值可参考 [cockpit-tools](https://github.com/jlcodes99/cockpit-tools) 开源仓库 `src-tauri/src/modules/oauth.rs`，或在 Google Cloud Console 自建 OAuth 客户端。

## 数据与安全

- 所有凭据只存在本机：OpenCode/Cockpit/官方客户端的文件不属于本工具；本工具自己添加的账号存 `~/.quota-cli/accounts.json`，环境变量存 `~/.quota-cli/config.json`
- 不上传任何数据，无遥测
- Web UI 只绑定 127.0.0.1
- 终端输出对密钥脱敏（只显示尾 4 位）

## Agent / Skill

仓库附带 `skills/quota-cli/SKILL.md`，复制到 `~/.config/opencode/skills/quota-cli/` 即可让 OpenCode 等代理识别本工具。

## 已知限制

- 各家额度接口均为非公开接口，随时可能变化；返回为空时工具会如实显示
- OpenAI 账号需已登录 OpenCode 才能查询
- Antigravity OAuth 需自行配置 Google client 凭据（见上文）
- Windows Terminal / 支持 ANSI 的终端体验最佳

## License

MIT
