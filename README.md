# Quota CLI

聚合各 AI 平台账号额度的本地工具。一个 Python 脚本，把多个平台的剩余额度、套餐、订阅周期汇总到一处，用终端、桌面悬浮窗、本机 Web UI 三种方式展示。

## 工作逻辑

1. **认证发现**：从本机各 AI 客户端的登录态、环境变量、手动添加的 API Key 中收集账号，按身份与密钥去重。
2. **额度拉取**：按平台并行查询剩余额度、套餐、订阅起止时间。
3. **凭证保鲜**：所有账号快照进本地凭证库 `agent.db`（SQLite）；OAuth 令牌过期前 60 秒或遇到 401 时自动用 refresh token 换新，新票写回凭证库，并同步回来源客户端（refresh 轮换的平台必须回写，否则链条中断）。
4. **展示**：
   - 终端看板（动态刷新）
   - 桌面悬浮窗（置顶、可拖动缩放、调透明度、托盘图标，任务栏无显示）
   - 本机 Web UI（`127.0.0.1:18765`，响应式卡片、平台筛选、用量/到期榜、四款主题、添加账号、OAuth 授权、重置 OpenAI 额度）
5. **分发（可选）**：把凭证库里的账号写回各 CLI / IDE（OpenCode、OMP、Codex、Claude Code、Kimi Code、Grok CLI、Cursor、Antigravity），写入前自动刷新令牌、备份目标、冲突确认、记录历史。

## 启动

要求 Python 3.10+。

```
git clone https://github.com/dushanaicode/quota-cli.git
cd quota-cli
pip install -r requirements.txt
```

Windows 运行 `install.cmd`（加入 PATH），macOS / Linux 运行 `sh install.sh`，之后任意终端：

```
quota            # 交互菜单：查看 / 动态刷新 / 账号管理 / 配置 / Web UI / 悬浮窗 / 写入 harness
quota ui         # 打开本机 Web UI，网页里添加账号、走 OAuth
quota float      # 启动桌面悬浮窗
quota show       # 终端动态刷新额度
quota show --once  # 查一次就退出（脚本 / Agent 用）
quota add        # 交互式添加账号
quota accounts   # 查看本地账号库
quota config     # 查看配置
```

Antigravity 的 Google OAuth 需先在 Google Cloud Console 自建 OAuth 客户端，然后：

```
quota env QUOTA_AGY_CLIENT_ID <id>
quota env QUOTA_AGY_CLIENT_SECRET <secret>
```

## 支持平台

| 平台 | 额度内容 | 认证方式 |
|---|---|---|
| Grok / xAI | 周额度、高频/普通任务、套餐、订阅周期 | OAuth / API Key / 本机登录 |
| OpenAI | 5h/周/月窗口、重置次数、套餐 | OAuth |
| Claude Code | 额度窗口 | OAuth / 本机登录 |
| Zhipu / Z.ai | 5h/周/月窗口 | API Key |
| Kimi Code | 周/5h 窗口 | API Key |
| DeepSeek | 余额（CNY/USD） | API Key |
| Antigravity | Gemini / Claude+GPT 周/5h 窗口、套餐 | Google OAuth |
| Cursor | 用量汇总 | IDE 登录态 |
| Cursor Agent | 用量汇总、套餐、订阅周期 | API Key（`crsr_`）/ 本机登录 |

## 数据与安全

- 凭据只存本机（`~/.quota-cli/`），不上传任何数据，无遥测
- Web UI 只绑定 127.0.0.1
- 界面对密钥脱敏显示

## Agent / Skill

仓库附带 `skills/quota-cli/SKILL.md`，复制到 `~/.config/opencode/skills/quota-cli/` 即可让 Agent 识别本工具的全部能力（查询、添加账号、写入 harness、重置额度等 API）。

## License

MIT
