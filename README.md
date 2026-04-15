# review_ai_robots

一个基于飞书长连接模式的 AI 运维/研发机器人，支持群聊命令式协作，覆盖 SQL 审核执行、Gitea PR 自动审查、封版控制、远程服务巡检、测试计划流转和代码审查日报。

项目当前以 Windows 运行和打包为主，源码方式建议使用 Python 3.11+。如果只运行源码，Linux 也可以使用大部分能力。

## 功能概览

- 飞书群聊机器人，支持 `@机器人` 和部分上下文回复
- SQL 审核与确认执行流程，支持按地区和数据库路由
- Gitea PR 自动审查，支持自动评论、自动合并、封版拦截
- 远程服务器 `supervisorctl` 管理和只读诊断命令
- PR 合并后自动生成测试要点，并在 BW 群推进验收
- 每日 21:30 自动发送团队代码审查日报
- 每日 19:30 按指定人员私聊发送个人代码审查日报（默认汇总当天）
- 支持 Anthropic、OpenAI 兼容接口和 ChatGPT 三种 AI 后端

## 目录说明

```text
ai/          AI 调用封装、Prompt、SQL 工具
handlers/    飞书消息路由、SQL/PR 审核、日报、测试计划
infra/       飞书、Gitea、SSH、数据库、统计存储
tools/       本地命令、远程命令、SQL 执行器
data/        运行期 SQLite 数据
main.py      程序入口
build_exe.ps1 Windows 打包脚本
```

## 快速开始

### 1. 克隆仓库

```powershell
git clone https://github.com/ifczt/review_ai_robots.git
cd review_ai_robots
```

### 2. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 准备配置文件

复制示例文件并填写真实配置：

```powershell
Copy-Item .env.example .env
Copy-Item db_connections.toml.example db_connections.toml
Copy-Item ssh_servers.toml.example ssh_servers.toml
```

如果你要使用 `/svc`，还需要把 SSH 私钥放到项目根目录，例如 `IFCZT_KEYS`，或在 `ssh_servers.toml` 中改成绝对路径。

### 4. 配置飞书应用

需要在飞书开放平台创建自建应用，并开启机器人能力。

最少准备：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

建议确保机器人具备读取群消息、发送消息等权限，并把机器人拉入目标群聊。

### 5. 启动机器人

```powershell
python main.py
```

启动后机器人会：

- 建立飞书长连接
- 启动每日 21:30 的日报调度线程
- 在收到群聊消息时按命令路由处理

## 核心命令

| 命令 | 说明 |
| --- | --- |
| `/sql <地区>.<库名> <SQL>` | 触发 SQL 审核，审核通过后等待人工确认执行 |
| `/gitreview <PR链接>` | 审查 Gitea PR，通过时可自动合并 |
| `/freeze on <项目> [原因]` | 开启项目封版，只影响指定项目（格式：`repo` 或 `owner/repo`） |
| `/freeze off <项目>` | 解除指定项目封版 |
| `/freeze <项目>` | 查看指定项目封版状态 |
| `/freeze` | 查看全局与所有项目封版状态 |
| `/svc <地区> <子命令>` | 远程执行 `supervisorctl` |
| `/run <命令>` | 执行本地只读诊断命令 |
| `/testlist` | 查看当前待测试任务 |
| `/report` | 立即发送今日日报 |
| `/preport` | 立即发送个人日报（默认发送今天） |
| `/chatid` | 获取当前群 `chat_id`，方便写入配置 |
| `/clear` | 清空当前用户对话历史 |
| `/help` | 查看帮助 |

常见示例：

```text
/sql sa.user SELECT * FROM users WHERE id = 1
/gitreview https://gitea.example.com/owner/repo/pulls/42
/freeze on bigwin_admin 发布前封版
/svc sa status
/svc sa restart grpc_notice_hook
/run git status
```

## 配置说明

### `.env`

主要配置项如下：

- `AI_BACKEND`: `anthropic` / `openai` / `chatgpt`
- `ANTHROPIC_API_KEY`: Anthropic Key
- `OPENAI_API_KEY`: OpenAI 兼容接口 Key
- `CHATGPT_API_KEY`: OpenAI GPT 系列 Key
- `GITEA_BASE_URL`: Gitea 服务地址
- `GITEA_TOKEN`: Gitea Token，至少需要仓库读取和评论/合并权限
- `BW_CHAT_ID`: BW 测试群的 `chat_id`
- `DAILY_REPORT_CHAT_ID`: 团队日报接收群 `chat_id`
- `DAILY_REPORT_PRIVATE_RECIPIENTS`: 个人日报接收映射，JSON 格式 `{"gitea_author":"feishu_open_id"}`
- `DAILY_REPORT_SEND_HOUR`: 个人日报发送小时，默认 `19`
- `DAILY_REPORT_SEND_MINUTE`: 个人日报发送分钟，默认 `30`
- `DAILY_REPORT_LOOKBACK_DAYS`: 个人日报回看天数，默认 `0`（即发送今日日报）
- `SSH_KEY_PASSPHRASE`: 私钥密码，可为空

### `db_connections.toml`

用于配置多地区数据库连接，格式为 MySQL DSN：

```toml
[sa]
user = "mysql://username:password@db.example.com:3306/user"
```

### `ssh_servers.toml`

用于配置多地区运维机连接信息：

```toml
[sa]
host = "ops.example.com"
port = 22
user = "ubuntu"
key_path = "IFCZT_KEYS"
```

## 运行机制

### SQL 审核

1. 用户发送 `/sql` 或 `地区.库名: SQL`
2. 本地先做危险关键字拦截
3. AI 结合表结构工具进行审核
4. 机器人展示结论
5. 用户回复“确认执行”后才真正落库

### PR 审查

1. 用户发送 `/gitreview <PR链接>`
2. 机器人拉取 PR 信息和 diff
3. AI 输出 `[APPROVE]` / `[REQUEST_CHANGES]` / `[REJECT]`
4. 通过时自动合并，不通过时回写 Gitea 评论
5. 如果目标项目处于封版状态，则仅允许 BUG 修复类 PR 继续自动合并
6. 合并成功后可自动创建测试计划并通知 BW 群

### 每日审查日报

- 数据保存在 `data/stats.db`
- 每晚 21:30 汇总当天 PR 审查记录
- 由 AI 生成人员工作总结并发送到配置群聊

## 打包为 EXE

项目自带 Windows 打包脚本：

```powershell
.\build_exe.ps1
```

打包后会在 `dist/` 目录生成 `bot.exe`，并复制运行所需配置文件。

## 安全建议

- 不要把 `.env`、`db_connections.toml`、`ssh_servers.toml`、私钥文件提交到仓库
- 建议给数据库账户最小权限
- 建议给 Gitea Token 配置最小必需权限
- `/run` 和 `/svc` 已做白名单限制，但仍建议只在可信群内使用

## 已知说明

- `main.py` 中单实例锁在 Windows 上使用系统 Mutex；非 Windows 环境会自动跳过
- `build_exe.ps1` 为 Windows PowerShell 脚本
- `data/stats.db` 为运行期文件，不建议纳入版本控制

## License

如需开源发布，建议补充明确的 License 文件后再对外推广。
