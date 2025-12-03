# 带派聊天室 0.1 | 开发与运维指南

本项目是一个基于 Flask + Flask-SocketIO 的轻量聊天室，支持：
- 实时聊天（Socket.IO，已强制长轮询，适配受限网络）
- AI 流式回复（SSE）
- 天气、新闻、小视频、音乐搜索等功能标签
- 诊断页与健康检查，便于公网/内网穿透场景排障

目录结构（关键项）
- `app.py`：后端入口与所有路由/事件
- `templates/`：`login.html`、`chat.html`、`diagnostics.html`
- `config.json`：AI/天气与服务器列表配置
- `requirements.txt`：依赖清单


## 快速启动

环境要求：
- Python 3.11（已内置 `venv` 目录可用）
- Windows（示例命令为 PowerShell）

安装依赖并启动：
```
# 如使用系统 Python：
python -m venv venv
./venv/Scripts/activate
pip install -r requirements.txt

# 启动（默认端口 15000；可用 PORT 覆盖）
$env:PORT=15200; python app.py
```

启动日志示例：
- `wsgi starting up on http://0.0.0.0:15200` 表示监听成功
- 如遇 `WinError 10048` 端口占用，可执行：
```
netstat -ano | findstr :15200
# 结束占用进程（替换为实际 PID）
powershell -Command "Stop-Process -Id <PID> -Force"
```

本机预览：
- 登录页：`http://localhost:15200/`
- 聊天页：`http://localhost:15200/chat`
- 诊断页：`http://localhost:15200/diagnostics`

默认登录密码：`123456`。
服务器地址列表在登录页供选择。


## 配置方式

`config.json` 示例：
```
{
  "servers": [
    { "name": "本地局域网", "url": "http://YOUR_LAN_IP:15200" },
    { "name": "公开网", "url": "http://8240qwp2i10w.ngrok.xiaomiqiu123.top" }
  ],
  "ai": {
    "api_key": "<你的密钥>",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "Qwen/Qwen2.5-7B-Instruct"
  },
  "weather": {
    "api_key": "<你的OWM密钥>",
    "base_url": "https://api.openweathermap.org"
  }
}
```

环境变量优先覆盖文件配置：
- AI：`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`
- 天气：`OWM_BASE_URL`、`OWM_API_KEY`
- 端口：`PORT`（默认 `15000`）

安全建议：生产环境将敏感密钥放入环境变量，不要提交到仓库。


## 协议与服务地址清单（用于内网穿透映射）

所有路径默认在同一 HTTP 端口（`PORT`，建议 15200），公网域名应一一映射到本机端口。

- 站点页与表单
  - `GET /` → 登录页（`login.html`）
  - `POST /login` → 登录提交（字段：`nickname`、`password`、`server`）
  - `GET /chat` → 聊天页（`chat.html`）
  - `GET /logout` → 退出登录

- 健康与诊断
  - `GET /health` → 健康检查（JSON），用于隧道与端口验证
  - `GET /diagnostics` → 诊断页（前端连通性与 Socket.IO 测试）

- 实时通道（Socket.IO）
  - 路径：`/socket.io`（客户端配置 `path='/socket.io'`）
  - 传输：已强制 `transports=['polling']`、`upgrade=false`（禁用 websocket 升级）
  - 握手测试：`/socket.io/?EIO=4&transport=polling`
  - 事件：
    - `join`（加入房间）
    - `send_message`（发送消息）
    - `disconnect`（断开连接）

- AI 流式（SSE）
  - `GET /ai/stream?q=...`（`text/event-stream` 长连接）
  - 需隧道允许长连接与禁缓存；前端使用 `EventSource`

- 功能接口（服务端向互联网请求数据）
  - 天气：`GET /feature/weather?q=@天气 …`（上游 `api.openweathermap.org`）
  - 音乐搜索：`GET /feature/music/search`（上游 `itunes.apple.com`）
  - 新闻：`GET /feature/news`（上游 `v2.xxapi.cn/api/weibohot`）
  - 小视频：`GET /feature/video`（上游 `v2.xxapi.cn/api/meinv`）

注：以上「上游」为服务端外网访问，不需要内网穿透，但本机需能访问互联网。


## 公网访问与内网穿透映射

目标：让公网域名（例如 `http://your.ngrok.domain`）映射到本机 `PORT` 端口。

必须满足：
- 公网隧道使用 `http://` 协议（避免浏览器自动升级 `https://` 导致混合内容/跨域问题）
- 允许 `GET/POST` 访问 `'/socket.io'` 路径（长轮询）
- 允许长连接访问 `'/ai/stream'` 路径（SSE）

建议步骤：
1) 本机启动服务：`$env:PORT=15200; python app.py`
2) 隧道映射：公网端口（如 80/8080/15200）→ 内网 `你的IP:15200`
3) 诊断页验证：`http://your.ngrok.domain/diagnostics`
   - 健康检查/接口/轮询握手/客户端连接（轮询）应全部成功
4) 登录页选择你的公网域名，密码 `123456`，进入聊天页测试即可

自检 URL：
- `http://your.ngrok.domain/health`
- `http://your.ngrok.domain/socket.io/?EIO=4&transport=polling`
- `http://your.ngrok.domain/diagnostics`

常见问题：
- 选了 `127.0.0.1`：其他人无法连接。应选择你的公网域名。
- 端口不一致：隧道映射到 `15200`，但服务跑在 `15000`。请统一 `PORT` 与隧道端口。
- 网络拦截 websocket：已强制使用长轮询，通常可用；若仍失败，换网络（热点/家用）。
- `WinError 10048` 端口占用：用 `netstat` 定位并停止占用进程。


## 前端行为与功能标签

- 布局：仅消息区允许滚动；底部工具区固定。
- Socket.IO：`polling` 传输、自动重连；出错显示“服务不可用”。
- AI 流式：`@成小理` 触发，前端连接 `/ai/stream` 并渲染流。
- 天气：`@天气 上海 明天` → `/feature/weather`
- 新闻：`@新闻` → `/feature/news`
- 小视频：`@小视频` → `/feature/video`
- 音乐搜索：
  - `@音乐 搜索 关键词`
  - `@音乐一下 歌手 名称`
  - `@音乐一下 歌名 名称（+歌手）`


## 开发工作流（建议）

1) 配置 `config.json` 或环境变量，确保 AI/天气密钥与服务器列表可用。
2) 启动并运行诊断页，确保所有测试通过（至少“轮询连接”成功）。
3) 在本机或公网域名登录，验证消息收发与功能标签工作正常。
4) 若需改端口或域名，同步更新启动命令与隧道映射；`servers` 可保留公网域名项。
5) 观察终端日志，定位接口或上游错误；必要时提高日志级别或加限流。


## 开发日志（dev.log）

- 路径：`logs/dev.log`（自动创建与滚动备份）
- 内容：按行记录 JSON（JSON Lines），包含时间戳、类别、消息与上下文
- 记录点：登录页/聊天页打开、登录成功/失败、Socket.IO 事件、AI 流式/非流式调用、健康/诊断页访问等
- 示例行：
  - `{"ts":"...","level":"INFO","category":"event","message":"login_success","context":{"nickname":"Alice"}}`
  - `{"ts":"...","level":"INFO","category":"prompt","message":"send_message","context":{"nickname":"Alice","msg":"@成小理 介绍下成都"}}`

使用建议：
- 研发记录：将关键帮助信息/提示词直接以消息发送（@标签），便于日志留存与复盘
- 排障定位：结合诊断页与 `dev.log` 快速归因网络与上游问题
- 敏感信息：必要时做字段脱敏或禁写入（修改 `dev_log` 实现即可）


## 测试示例

- 普通消息：`你好`
- AI：`@成小理 介绍下上海的地标`
- 天气：`@天气 上海 明天`
- 新闻：`@新闻`
- 小视频：`@小视频`
- 音乐：`@音乐 搜索 周杰伦`


## 说明
- 本项目为学习/演示用途，密钥与外部接口可能存在访问频率限制。
- 如部署到生产，请配置 HTTPS、限流、鉴权与日志/监控。
- 欢迎按需扩展：聊天记录持久化、联系人管理、文件/图片消息等。

