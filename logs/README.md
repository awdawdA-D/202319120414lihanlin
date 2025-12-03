# 开发日志说明

本目录用于记录开发过程中的关键事件、帮助信息、提示词等内容，便于后续分析与追踪。

写入格式：按行记录 JSON（JSON Lines），每条日志一行，示例：
```
{"ts":"2025-01-01T12:00:00Z","level":"INFO","category":"event","message":"server_started","context":{"port":15200}}
{"ts":"2025-01-01T12:05:00Z","level":"INFO","category":"prompt","message":"@成小理 介绍下上海","context":{"nickname":"Alice"}}
{"ts":"2025-01-01T12:06:00Z","level":"INFO","category":"help","message":"检查 /diagnostics 页面轮询连接","context":{}}
{"ts":"2025-01-01T12:07:00Z","level":"WARN","category":"tip","message":"更换网络以绕过 websocket 阻断","context":{"network":"mobile"}}
```

类别建议：
- `event`：系统/业务事件（启动、加入房间、发送消息等）
- `prompt`：用户提示词或 AI 相关输入
- `help`：帮助信息或操作指引
- `tip`：优化建议、踩坑提示

文件：
- `dev.log`：主开发日志（自动按代码写入）

注意：日志可能包含敏感信息，请谨慎对外共享；生产环境可按需开启或脱敏。

