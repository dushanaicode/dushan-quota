---
name: quota-cli
description: 查询并管理本机 Grok/OpenAI/Claude/Zhipu/Kimi/DeepSeek/Antigravity/Cursor 额度。用户提到额度、quota、套餐、认证账号、添加 API Key、环境变量时使用。
---

# Quota CLI Skill

轻量终端额度看板。认证读取顺序：OpenCode auth.json → Cockpit（需 pycryptodome）→ 本机官方目录 → quota-cli 本地库 → 环境变量。

## 安装（首次）

```
git clone https://github.com/<owner>/quota-cli.git
cd quota-cli
pip install -r requirements.txt
# Windows: 运行 install.cmd
# macOS/Linux: sh install.sh
```

## Agent 命令（不要进交互菜单）

查询额度（唯一正确的查询入口）：

```
python <repo>/quota.py show --once
```

账号管理：

```
python <repo>/quota.py accounts
python <repo>/quota.py add zai --key <API_KEY>
python <repo>/quota.py add kimi --key <API_KEY>
python <repo>/quota.py add deepseek --key <API_KEY>
python <repo>/quota.py add grok --local
python <repo>/quota.py add zai --env
python <repo>/quota.py remove <id>
```

平台：`grok` `openai` `claude` `zai` `kimi` `deepseek` `antigravity` `cursor`

OAuth（浏览器授权，人操作）：`python <repo>/quota.py ui` → 添加账号。支持 Grok（xAI device flow）、Antigravity（Google）、Cursor。

环境变量（写入 ~/.quota-cli/config.json，可选 --user 写入系统用户变量）：

```
python <repo>/quota.py env ZHIPU_API_KEY <value>
python <repo>/quota.py env KIMI_API_KEY <value> --user
```

可用变量：`XAI_API_KEY` `ZHIPU_API_KEY` `ZAI_API_KEY` `KIMI_API_KEY` `DEEPSEEK_API_KEY` `OPENAI_API_KEY` `ANTHROPIC_API_KEY`

## 规则

- 永远不要把密钥/token 写进回复或文件
- 查额度只用 `show --once`，不要进交互菜单
- 缺认证时先 `add` 或 `env`，不要修改 OpenCode/Cockpit 源码
- 余额/额度接口是各家非公开接口，返回为空时如实报告，不要编数据
