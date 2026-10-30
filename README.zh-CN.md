<div align="center">

# LumenBot

**为 Telegram 社区打造更好的入口。**

面向 Telegram 社区的开源入群验证与访问控制机器人，专为无服务器部署设计。

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Vercel](https://img.shields.io/badge/Deployed%20on-Vercel-000000?logo=vercel&logoColor=white)](https://vercel.com/)
[![Upstash](https://img.shields.io/badge/Redis%20%2B%20QStash-Upstash-00C98D?logo=upstash&logoColor=white)](https://upstash.com/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue)](LICENSE)

</div>

LumenBot 提供完整的入群引导：资料检查、X 关注验证、人工审核和超时处理。通过 Telegram Webhook 接收事件，使用 Redis 保存验证会话，并通过 QStash 实现可靠的超时回调。

## 功能特性

- **入群防护**：立即限制新成员发言，并显示验证卡片。
- **资料检查**：验证 Telegram 名字和头像。
- **X 关注验证**：核验用户提交的 X 账号是否关注指定账号。
- **管理员操作**：群内当前管理员可直接在验证卡片上批准或拒绝。
- **超时处理**：超过可配置的验证时限后，自动移出未完成验证的成员。
- **并发会话**：使用 Redis 原子状态转移，协调 Telegram 和 QStash 回调。
- **接口保护**：校验 Telegram Secret、QStash 签名和管理接口密钥。
- **多群支持**：支持群白名单以及独立的用户验证会话。

## 技术架构

```text
Telegram
  │  chat_member / message / callback_query
  ▼
Vercel FastAPI Webhook
  ├── Upstash Redis：验证会话、私聊绑定、原子状态转移
  ├── TwitterAPI.io：X 关注关系查询
  └── Upstash QStash：延迟超时回调
```

基于 Python 3.12、`python-telegram-bot` 20.8、FastAPI、HTTPX、Upstash Redis 和 QStash 构建，无需长轮询进程、内存会话或 APScheduler JobQueue。

## 使用前须知

1. **TwitterAPI.io 需要它自己的 API Key。** 不需要 X 官方开发者账号，但 TwitterAPI.io 不是永久免费、无需密钥的公共接口。额度、价格、可用性和数据时效以服务商当前规则为准。
2. **提交 X 用户名不能证明账号所有权。** LumenBot 只核验所提交账号的关注关系，不构成身份或 X 账号所有权认证。
3. **部署需要配置。** 导入仓库只是第一步，还需配置 Vercel、Upstash 和 Telegram Webhook。
4. Telegram 入群申请审批和 LumenBot 验证是两套流程。开启入群申请时，成员获批入群后才会开始机器人验证。
5. 请仅部署你拥有或获授权管理的机器人与群组，并自行评估第三方服务的条款与合规性。

## 快速部署

### 1. 准备服务与参数

- 在 [@BotFather](https://t.me/BotFather) 创建机器人，取得 Bot Token。
- 创建 Upstash Redis 数据库，记录 REST URL 和 REST Token。
- 在 Upstash QStash 页面记录 Token、Current Signing Key 和 Next Signing Key。
- 在 TwitterAPI.io 获取 API Key。
- 准备目标 X 用户名和超级群数字 ID。

### 2. 导入 Vercel

1. 将本仓库导入 [Vercel](https://vercel.com/new)。
2. Application Preset 选择 **FastAPI**。
3. Root Directory 保持 `./`，构建设置保持默认。
4. 使用稳定的生产域名，例如 `https://your-project.vercel.app`。不要使用单次部署的临时地址。

### 3. 配置环境变量

在 **Project → Settings → Environment Variables** 中为 **Production** 添加以下变量。修改后必须重新部署。

#### 必填

| 变量 | 说明 |
| --- | --- |
| `TG_TOKEN` | BotFather 提供的机器人 Token。 |
| `TARGET_X_ID` | 要求关注的 X 用户名，不带 `@`。 |
| `TWITTERAPI_KEY` | TwitterAPI.io 的 API Key。 |
| `ALLOWED_CHAT_IDS` | 启用验证的超级群数字 ID，通常以 `-100` 开头；多个 ID 用英文逗号分隔。留空表示所有群启用，生产环境建议明确配置。 |
| `TELEGRAM_WEBHOOK_SECRET` | Telegram Webhook 请求鉴权密钥。 |
| `ADMIN_SETUP_SECRET` | `/api/setup` 和 `/api/setup/status` 的管理密钥。 |
| `PUBLIC_BASE_URL` | 稳定生产域名，例如 `https://your-project.vercel.app`，不包含路径或末尾 `/`。也可使用 Vercel 的 `VERCEL_PROJECT_PRODUCTION_URL`，但建议显式设置。 |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST URL。 |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST Token。 |
| `QSTASH_TOKEN` | QStash Token。 |
| `QSTASH_CURRENT_SIGNING_KEY` | QStash Current Signing Key。 |
| `QSTASH_NEXT_SIGNING_KEY` | QStash Next Signing Key。 |

两个随机密钥必须不同，仅使用字母、数字、`_`、`-`，长度为 16–256 字符。可在本机 PowerShell 分别生成：

```powershell
[guid]::NewGuid().ToString("N")
```

#### 可选

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VERIFICATION_TIMEOUT_SECONDS` | `300` | 整轮验证时限，单位为秒；10 分钟填写 `600`。 |
| `X_REQUEST_TIMEOUT_SECONDS` | `15` | 单次 X API 请求超时，与整轮验证时限无关。 |
| `MAX_X_ATTEMPTS` | `5` | 每轮验证允许的最大 X 查询次数。 |
| `X_RETRY_SECONDS` | `10` | 同一轮两次 X 查询的最短间隔。 |
| `X_API_FAIL_OPEN` | `false` | X 查询失败时是否跳过该项并放行。默认转人工审核；仅在明确接受降低防护时设置为 `true`。 |

机密值使用 Vercel 的 **Secret** 类型，普通配置可使用 **Config**。不要将真实密钥写入 `.env.example`、README 或提交到 Git。

### 4. 部署并注册 Webhook

环境变量保存后重新部署 Production。确保 Telegram 和 QStash 可以公开访问接口，不要让 Deployment Protection 要求交互式登录。

健康检查：

```text
https://your-project.vercel.app/api/health
```

正常响应应包含 `"ok":true`。

在本机 PowerShell 注册 Telegram Webhook，输入的是 `ADMIN_SETUP_SECRET` 的值，不是变量名：

```powershell
$base = "https://your-project.vercel.app"
$admin = Read-Host "ADMIN_SETUP_SECRET"

Invoke-RestMethod -Uri "$base/api/setup" `
  -Method Post `
  -Headers @{ "X-Admin-Setup-Secret" = $admin }

Invoke-RestMethod -Uri "$base/api/setup/status" `
  -Headers @{ "X-Admin-Setup-Secret" = $admin }
```

第一条应返回 `ok: True`，第二条应返回 `matches: True`，且 `last_error_message` 为空。

### 5. 设置 Telegram 群权限

将机器人添加为超级群管理员，授予：

- **封禁用户 / Ban Users**：用于禁言、解除禁言、超时踢出与解封。
- **删除消息 / Delete Messages**：用于清理验证和审核消息。

其他群管理权限不是必需的。Bot Privacy 不控制 `chat_member` 更新，但机器人必须是群管理员才能接收其他成员的这些更新。

机器人会通过 Telegram 实时确认操作者是否为当前群管理员或群主，无需配置管理员 ID 名单。`ALLOWED_CHAT_IDS` 是群 ID，必须保留开头的负号，不要填写群用户名或邀请链接。

## 验证流程

1. `chat_member` 检测到新成员后，机器人限制其发言，创建 Redis 会话和 QStash 延迟任务。
2. 群内显示关注链接和验证按钮；只有对应的新成员可以使用该验证会话。
3. 机器人检查 `first_name` 和 `get_user_profile_photos(...).total_count`。
4. 基础检查通过后，用户通过群内按钮进入私聊。Telegram 会发送带验证参数的 `/start`，用户随后直接发送 X 用户名。
5. 机器人调用 TwitterAPI.io。仅当响应严格包含 JSON 布尔值 `data.following: true` 时，X 项才自动通过。
6. 三项检查通过后解除限制并删除验证消息；未关注、账号不存在或资料不完整时，用户可按提示修正后重试。
7. X 接口故障时默认创建人工审核卡片。初始验证卡片也有一行独立的管理员批准/拒绝按钮。机器人实时校验操作者的群主或管理员身份，审核计时不会暂停。
8. 到期未完成时，机器人先检查成员状态，再执行封禁与解封，使用户之后仍可重新申请入群。已被管理员解除限制的普通成员不会被超时任务再次踢出。

每个验证会话只能对应一个群和一名用户。更换群后需从新群的当前验证卡片重新进入，旧链接不能跨群使用。

## 运维与故障排查

### 群内没有验证消息

- 确认机器人已在**当前群**担任管理员，并拥有封禁用户权限。
- 确认 `ALLOWED_CHAT_IDS` 与 Runtime Logs 中的真实 `chat_id` 一致，保留 `-100` 前缀。
- 用非群主、非管理员的测试账号退出后重新入群。
- 检查 Runtime Logs 是否出现“入群事件被群白名单跳过”或“新成员禁言失败”。

### 自动查询暂不可用

查看 Runtime Logs 中 `X 查询不可用 ...:` 后面的原因。通常与 API Key、额度、限流、第三方网络或响应结构有关。默认不会将查询失败当作已关注。

### `bot can't initiate conversation with a user`

机器人不能主动私聊从未启动过它、或已停止/屏蔽它的用户。该错误只影响主动私信提醒，例如超时通知，不代表 X 查询失败。

### 修改配置或域名

- 修改任何环境变量后，重新部署 Production。
- 仅修改验证时长、白名单等配置且域名未变时，无需重新注册 Webhook。
- 更换 `TELEGRAM_WEBHOOK_SECRET` 后，重新执行 `/api/setup`。
- `PUBLIC_BASE_URL` 决定新 QStash 任务的回调地址。更换生产域名后及时更新并重新部署；已经创建的旧任务不会自动迁移。

### 密钥安全

Telegram Bot API 请求 URL 可能包含 Bot Token，分享日志前必须遮挡。若 Token、管理密钥或其他凭据泄露，应立即撤销或轮换，再更新 Vercel 并重新部署。

### 已知边界

- 超时踢出依赖 QStash 回调；任务发布失败或 ban/unban 操作失败时，需要管理员按群内提醒处理。
- X 数据可能存在延迟、隐私限制或服务中断。
- 转人工审核或管理员手动操作不会延长验证时限。
- 本项目不代替完整的社区风控、反垃圾或身份认证系统。

## 本地开发与测试

安装生产依赖：

```bash
python -m pip install -r requirements.txt
```

安装测试工具：

```bash
python -m pip install "pytest>=8,<10" "pytest-asyncio>=0.23,<2" "PyJWT>=2.8,<3"
```

运行测试：

```bash
python -m pytest
```

本地启动 FastAPI：

```bash
python -m uvicorn guard.web:app --host 127.0.0.1 --port 3000
```

本地开发可将真实配置放在未跟踪的 `.env` 中。线上 Telegram/QStash 回调需要可公开访问的 HTTPS 地址；本地进程不能代替已经注册的 Vercel Webhook。测试使用内存 Redis 替身和 Mock HTTP，不等同于真实 Redis、Telegram、TwitterAPI.io 或 Vercel 的端到端验收。

## HTTP 接口

| 路径 | 方法 | 用途 |
| --- | --- | --- |
| `/` | GET | 基本服务状态页面。 |
| `/api/health` | GET | 健康检查，不返回密钥或群配置。 |
| `/api/setup` | POST | 管理密钥保护，注册 Telegram Webhook 并检查 Redis。 |
| `/api/setup/status` | GET | 管理密钥保护，查询 Webhook 状态。 |
| `/api/telegram` | POST | Telegram Secret 鉴权的 Update 入口。 |
| `/api/timeout` | POST | QStash 签名鉴权的超时任务入口。 |

## 目录说明

```text
main.py                Vercel ASGI 入口
api/index.py           API 入口
guard/config.py        环境变量与运行配置
guard/service.py       验证、审核、踢出与 Telegram 交互逻辑
guard/store.py         Redis 会话及原子 Lua 状态转移
guard/web.py           FastAPI 路由、Webhook 与签名校验
tests/                 自动化测试
pyproject.toml         Python 与 Vercel 项目配置
vercel.json            Vercel Function 配置
.env.example           环境变量示例，不包含真实密钥
```

## 参与贡献

欢迎提交 Issue 和 Pull Request。较大的行为变更请先说明使用场景和兼容性影响。请勿提交任何真实密钥，并测试涉及的验证、审核和超时流程。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。原作者及原项目声明见 [NOTICE](NOTICE)。使用或分发衍生作品时，请遵守许可证和 NOTICE 要求，并标明所作修改。
