# Quota CLI

聚合各 AI 平台账号额度的本地工具。一个 Python 脚本，把多个平台的剩余额度、套餐、订阅周期汇总到一处，用终端、桌面悬浮窗、本机 Web UI 三种方式展示。

## 技术栈

刻意做轻：后端 Python 3.10+，前端一份原生 HTML/CSS/JS，没有 React、没有 Node、没有 Tauri/Electron。

- 职责是查额度、保鲜令牌、写入各 CLI/IDE，不是 IDE 多开或切号桌面套件
- 依赖很少：`requirements.txt` 只有加解密和悬浮窗
- 页面是单文件，浏览器直接打开本机 `127.0.0.1:18765`

## 工作逻辑

1. **认证发现**：从本机各 AI 客户端的登录态、环境变量、手动添加的 API Key 中收集账号，按身份与密钥去重。
2. **额度拉取**：按平台并行查询剩余额度、套餐、订阅起止时间。CLI、Web UI、悬浮窗共用 `~/.quota-cli/quota-snapshot.json`；跨进程锁保证同一刷新周期只有一个进程联网，其余界面读取同一快照。
3. **凭证保鲜**：所有账号快照进本地凭证库 `agent.db`（SQLite）；OAuth 令牌过期前 60 秒或遇到 401 时自动用 refresh token 换新，新票写回凭证库，并同步回来源客户端（refresh 轮换的平台必须回写，否则链条中断）。
4. **展示**：
   - 终端看板（动态刷新）
   - 桌面悬浮窗（置顶、可拖动缩放、调透明度、托盘图标，任务栏无显示）
   - 本机 Web UI（`127.0.0.1:18765`，响应式卡片、平台筛选、用量/到期榜、四款主题、添加账号、OAuth 授权、重置 OpenAI 额度）
5. **分发（可选）**：把凭证库里的账号写回各 CLI / IDE（OpenCode、OMP、Codex、Claude Code、Kimi Code、Grok CLI、Cursor、Antigravity），写入前自动刷新令牌、备份目标、冲突确认、记录历史。

## 启动

要求 Python 3.10+。

### 用提示词安装（推荐）

把下面整段交给本机 Agent（Cursor / OpenCode / OMP 等）。装完后新开一个终端，直接打 `quota`。

```
请在本机安装并启用 Quota CLI。

仓库：https://github.com/dushanaicode/quota-cli

按顺序做完，不要改 Cockpit / OpenCode 源码，不要把密钥写进回复：

1. 确认 Python 3.10+ 可用（Windows 用 python 或 py -3，macOS/Linux 用 python3）。没有就先安装，并保证命令在 PATH 里。
2. 若还没有仓库，执行：git clone https://github.com/dushanaicode/quota-cli.git
3. 进入仓库根目录。
4. 安装并加入 PATH：
   - Windows：运行 install.cmd
   - macOS / Linux：sh install.sh
   脚本会安装 requirements.txt，把 quota 加入用户 PATH，并把 skills/quota-cli/SKILL.md 复制到 ~/.config/opencode/skills/quota-cli/。
5. 当前终端若还找不到 quota，把仓库目录（Windows）或 ~/.local/bin（macOS/Linux）临时加入 PATH，或新开一个终端。
6. 验证：
   - quota show --once
   - 能跑通即可；查额度失败若是没账号，不算安装失败。

完成后只报告：quota 命令的实际路径，以及 quota show --once 的退出码。
```

### 手动安装

```
git clone https://github.com/dushanaicode/quota-cli.git
cd quota-cli
```

Windows 运行 `install.cmd`，macOS / Linux 运行 `sh install.sh`。之后任意终端：

```
quota            # 交互菜单：查看 / 动态刷新 / 账号管理 / 配置 / Web UI / 悬浮窗 / 写入 harness
quota ui         # 后台启动本机 Web UI 并立即返回，网页里添加账号、走 OAuth
quota float      # 启动桌面悬浮窗；刷新时显示进度及快照状态
quota show       # 终端动态刷新额度
quota show --once  # 查一次就退出（脚本 / Agent 用）
quota show --once --force  # 明确忽略共享快照并立即联网刷新
quota add        # 交互式添加账号
quota accounts   # 查看本地账号库
quota config     # 查看配置
```

Web UI 卡片右上角的“✕”会先弹出确认框；确认关闭后，卡片进入右上角“历史”，并保留套餐、订阅周期等展示信息，不会删除账号、登录态或认证文件。历史记录支持逐个恢复或全部恢复，旧版隐藏记录会自动兼容。

Antigravity 的 Google OAuth 需先在 Google Cloud Console 自建 OAuth 客户端，然后：

```
quota env QUOTA_AGY_CLIENT_ID <id>
quota env QUOTA_AGY_CLIENT_SECRET <secret>
```

## 支持平台

| 平台 | 额度内容 | 认证方式 |
|---|---|---|
| Grok / xAI | 周额度、高频/普通任务、套餐、订阅周期 | OAuth / API Key / 本机登录 |
| OpenAI | 5h/周/月窗口、剩余重置次数、套餐、认证声明提供的订阅起止时间 | OAuth |
| Claude Code | 额度窗口 | OAuth / 本机登录 |
| Zhipu / Z.ai | 5h/周/月窗口 | API Key |
| Kimi Code | 周/5h 窗口 | API Key |
| DeepSeek | 余额（CNY/USD） | API Key |
| Antigravity | Gemini / Claude+GPT 周/5h 窗口、套餐 | Google OAuth |
| Cursor | 用量汇总 | IDE 登录态 |
| Cursor Agent | 用量汇总、套餐、订阅周期 | API Key（`crsr_`）/ 本机登录 |

OpenAI 的订阅周期按 Cockpit Tools 的只读流程查询：先从 `accounts/check` 选择当前账号/组织的 entitlement，再用 `subscriptions` 补充生效时间及过期数据，最后才回退到 Codex 本地 ID token。额度窗口的 `reset_at` 不会被当作订阅到期时间；Free 账号若存在历史付费订阅，会保留并显示其真实到期日。

## 数据与安全

- 凭据只存本机（`~/.quota-cli/`），不上传任何数据，无遥测
- 共享额度快照只保存展示字段，不保存 access token、refresh token 或 API Key
- Web UI 只绑定 127.0.0.1
- 界面对密钥脱敏显示

## Agent / Skill

仓库附带 `skills/quota-cli/SKILL.md`，复制到 `~/.config/opencode/skills/quota-cli/` 即可让 Agent 识别本工具的全部能力（查询、添加账号、写入 harness、重置额度等 API）。

## License

MIT
