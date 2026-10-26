# feishu_cursor

飞书机器人把自然语言转给本机 Cursor Agent。绑定工作目录后，可以像在 Cursor IDE 里一样改代码；在明确允许后，Agent 才能对**当前绑定目录**提交代码、建立 GitLab 仓库或分支。

## 功能

| 指令 | 说明 |
|------|------|
| `/cursor` | 进入 Cursor 对话（Auto）。再次进入会续上当前对话 |
| `/cursor_used` | 查询当前 Cursor 已使用的额度 |
| `/exit` | 退出对话模式，保留记忆 |
| `/bind {工作文件夹}` | 绑定 `/home/uisee/workspace` 下的工程目录 |
| `/query_current_workspace` | 查询当前工作文件夹。未绑定时返回默认目录 |
| `/unbind {工作文件夹}` | 解绑，并关闭 GitLab MCP |
| `/new_chat` | 新建一段对话 |
| `/query_chat` | 列出本会话的全部对话，带主题和最近一句提问 |
| `/enter_chat {chatid}` | 跳转到某一段对话，支持唯一前缀 |
| `/query_list` | 列出当前目录下一层文件和子目录的绝对路径。未绑定时列出 `/home/uisee/workspace` |
| `/allow_mcp` | 允许 GitLab 操作，且仅限当前绑定目录。未绑定则直接拒绝 |
| `/forbid_mcp` | 禁用 GitLab 提交、建仓库、建分支 |

换绑或解绑后需要重新执行 `/allow_mcp`。

## 配置

凭证放在 `secret_config.yaml`（不要提交到 git）。可从示例复制：

```bash
cp secret_config.yaml.example secret_config.yaml
```

```yaml
feishu_app_id: cli_xxx
feishu_app_secret: your_app_secret
gitlab_url: https://gitlab.example.com
gitlab_token: your_gitlab_token
gitlab_username: your_gitlab_username
git_author_name: your_name
git_author_email: you@example.com
cursor_api_key: crsr_xxx
cursor_agent_model: auto
cursor_agent_timeout: 1800
```

容器内没有 Cursor 登录态时必须填写 `cursor_api_key`。

飞书开放平台还需：

- 事件订阅使用长连接，并订阅 `im.message.receive_v1`
- 开通发消息权限，发布应用
- 开通 `application:app_slash_command:read` 和 `application:app_slash_command:write`，以便同步输入框快捷指令

## 避免 OpenClaw 抢走消息

本服务和 OpenClaw 如果使用同一个飞书应用，长连接只会把消息随机交给其中一个客户端。OpenClaw 仍在线时，它会用自己的回复打断 Cursor Agent。

要让消息只进本服务，到云终端关闭 OpenClaw 的飞书通道：

```bash
openclaw config set channels.feishu.enabled false
openclaw gateway restart
```

## 部署

在项目目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
```

`docker-compose.yml` 会把 `secret_config.yaml` 只读挂到容器内 `/app/secret_config.yaml`。

部署约定：

- 镜像 `ubuntu:20.04`，容器用户 `uisee`（uid 1000）
- 主机 `/home/uisee/workspace` 挂到容器同路径和 `/workspace`
- `network_mode: host`
- 挂载本机 Cursor Agent（`~/.local/share/cursor-agent`、`~/.cursor`）
- 挂载 `/var/run/docker.sock`，Agent 可以 `docker exec` 进主机上的编译容器

飞书客户端的 `/` 指令列表大约 5 分钟后更新。命令本身在容器启动后即可直接发送。
