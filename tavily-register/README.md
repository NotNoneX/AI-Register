# tavily-register

## 目录结构

- `main.py`：批量任务入口（CLI），负责邮箱生成、调用注册流程、验证与保存结果
- `signup.py`：核心注册/登录/取 Key 逻辑（`requests.Session` 驱动）
- `mail_provider.py`：统一邮箱提供商接口（支持 `outlook_tw` 和 `luckmail`）
- `outlook_tw_provider.py`：`outlook.tw` 匿名临时邮箱客户端

## 环境要求

- Python `>= 3.12`
- 推荐使用 `uv` 管理依赖与虚拟环境

## 安装

```bash
uv sync
```

## 配置

### 1) `config.yaml`

`signup.py` 会从仓库根目录读取 `config.yaml`（已在 `.gitignore` 中忽略）。现在验证码识别默认走 YesCaptcha。示例：

```yaml
# YesCaptcha key
YESCAPTCHA_CLIENT_KEY: "YOUR_YESCAPTCHA_KEY"

# 代理 API 配置 (配置后自动轮换 IP；每个 IP 累计 3 次网络失败或 10 次注册尝试后提取新 IP)
PROXY_API_URL: "https://white.novproxy.com/white/api?region=US&num=1&time=8&format=1&type=txt"

# 临时邮箱提供商: outlook_tw (默认) 或 luckmail
EMAIL_PROVIDER: "outlook_tw"
```

也支持通过环境变量提供：

- `YESCAPTCHA_CLIENT_KEY`
- `YESCAPTCHA_KEY`

### 2) 临时邮箱环境变量（可选）

支持通过环境变量配置邮箱提供商：

- `EMAIL_PROVIDER`: `outlook_tw`（默认）或 `luckmail`
- `OUTLOOK_TW_BASE_URL`: `https://outlook.tw`
- `OUTLOOK_TW_USERNAME_LENGTH`: 8

`outlook.tw` 首次访问受保护接口时会显示 Cloudflare Turnstile。脚本只在收到
`captcha-required` 后读取响应中的 `sitekey`，通过 YesCaptcha 的
[`TurnstileTaskProxyless`](https://yescaptcha.atlassian.net/wiki/spaces/YESCAPTCHA/pages/61734913/TurnstileTaskProxyless+CloudflareTurnstile)
获取 token，再提交到 `/api/captcha`。验证成功后的
`mf-captcha` Cookie 会保存在批次共享的邮件 Session 中，后续随机邮箱生成和
收件箱轮询不会重复创建打码任务。此流程复用上面的 `YESCAPTCHA_CLIENT_KEY`
配置，不需要启动 Chrome。

## 运行

查看参数：

```bash
uv run python main.py --help
```

批量注册：

```bash
uv run python main.py
```

脚本默认带 Tavily 单 IP 限速保护：每完成 10 个注册，会自动等待 60 分钟再继续，避免触发平台限制。也可以手动调整：

```bash
uv run python main.py -n 20 --max-per-window 10 --window-seconds 3600
```

如果你没有把 YesCaptcha key 写进 `config.yaml`，也可以直接用环境变量运行：

```bash
YESCAPTCHA_CLIENT_KEY=your_yescaptcha_key uv run python main.py
```

### 网络重试与 IP 轮换

- Tavily/Auth0 注册链路的每个 HTTP 节点最多执行 3 次（首次请求 + 2 次重试）。
- 同一出口 IP 的传输层网络异常跨节点累计；达到 3 次后立即废弃该 IP。
- 轮换 IP 时保留当前邮箱、密码和已取得的验证链接，并从当前账号的最近状态继续，不会直接跳到下一个邮箱。
- 同一 IP 仍保留最多 10 次注册尝试限制；任一阈值先达到都会触发轮换。
- Outlook、LuckMail、YesCaptcha、代理提取和出口 IP 检测使用独立的 3 次网络重试，不计入 Tavily 出口 IP 的失败额度。
- YesCaptcha 等打码服务使用专用直连 Session，不使用 Tavily 注册代理，并忽略系统中的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境代理。



## 输出文件

- `api_keys.txt`：成功记录（API Key 列表）
- `failed.txt`：失败记录（邮箱与错误信息）
- `run.log`：运行日志（开始处理、成功、失败、进入 90 分钟等待、恢复时间等）

## 常见问题

- `ip-signup-blocked`：表示当前出口 IP 被禁止注册。配置代理 API 时脚本会轮换 IP 并继续当前账号；未配置代理时记录失败。
- `invalid-captcha`：验证码识别结果不正确。可更换 YesCaptcha key、降低并发、增加重试间隔
- `tavily`调整了策略，一个ip一段时间内只能注册5个，请勿滥用

## 资源推荐

- [Captcha.run](https://captcha.run/sso?inviter=542f4f4f-31b6-4b70-b485-c4762c45d1e8)（打码平台，强烈推荐）
- [YesCaptcha](https://cutt.ly/Mywt39r0)（自动验证码识别工具，便宜，好用）
- [订阅合租拼车](https://cutt.ly/5ywt8vb4)（国外合租平台，可以合租各种影视会员、AI订阅）
- [海外账号、电话卡](https://cutt.ly/dywt86NC)（TG账号、TikTok账号等等海外平台账号）
- [满血CC、GPT中转站](https://cutt.ly/JywJG3G5)（可以确认不掺水，缺点是价格偏高）
- [Telegram 搜索机器人](https://cutt.ly/2yeh3GOE)(TG 最强搜素引擎，试试看吧)
- [比特指纹浏览器](https://client.bitbrowser.cn/register?lang=zh&code=Alan123)（艾伦日常使用的指纹浏览器，挺好用的，没什么硬伤）

## 🚀 GPT 代充值平台

[艾伦の代充](https://ai.corouter.cc) 支持使用卡密自动完成 ChatGPT 等 AI 订阅代充，客户无需注册登录。

### 合伙人机制

- **邀请制加入**：使用平台签发的一次性邀请码注册，获得独立的合伙人工作空间。
- **自主开展业务**：充值业务积分并配置支付资源，自主生成、分发卡密，客户凭卡密完成充值。
- **灵活接入渠道**：既可直接销售卡密，也可通过 API 接入自己的站点。
- **数据独立管理**：每位合伙人的卡片、卡密、订单、积分和 API Token 相互隔离。
- [查看合伙人机制与参与指南](https://ai.corouter.cc/partner-guide)
