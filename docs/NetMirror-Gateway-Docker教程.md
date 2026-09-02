# NetMirror 网关 Docker 镜像 · 使用与部署教程

> 镜像地址：`ghcr.io/asxiaowen/netmirror-gateway`
> 维护：纯标准库 Python 反代网关（`gateway.py`，零第三方依赖）
> 适用：NetMirror Panel（面板）前置登录/访问控制，或 Agent（子节点）纯反代模式

---

## 1. 镜像信息

| 项目 | 值 |
|------|-----|
| 仓库 | `ghcr.io/asxiaowen/netmirror-gateway` |
| 标签 | `latest`、`20260902` |
| 当前 digest | `sha256:c25ea087202b377b20b4a7fdf2e7374a9d880bd0d81ffda1f3524de85a291bca` |
| 基础镜像 | `python:3-alpine`（仅用标准库，无需 pip 安装） |
| 镜像大小 | ≈ 48 MB |
| 代码位置（容器内） | `/app/gateway.py`（镜像自带，只读） |
| 数据位置（容器内） | `/data`（需挂载宿主机目录做持久化） |

**设计要点**：代码放在 `/app`，运行时数据放在 `/data`。二者分离，因此把宿主机目录挂到 `/data` 做持久化时，**永远不会覆盖到网关自身的代码**。（早期版本曾把代码放在 `/data`，导致空数据目录把代码本身遮蔽、容器起不来——现已修正。）

---

## 2. 拉取权限（重要）

该 ghcr 包目前是 **private**。在一台新机器上直接 `docker pull` 会报 `unauthorized`。二选一解决：

- **方式 A（推荐，保持私有）**：用具有 `read:packages` 权限的 GitHub PAT 登录：
  ```bash
  docker login ghcr.io -u asxiaowen -p <你的PAT>
  ```
- **方式 B（完全公开）**：在 GitHub → 个人头像 → **Packages** → `netmirror-gateway` → **Package settings** → **Change visibility** → 设为 **Public**。之后任意机器无需登录即可 `docker pull`。
  > **注意**：ghcr.io 容器包的可见性目前只能通过 GitHub Web UI 修改，用 REST API `PATCH /user/packages/container/.../visibility` 会返回 404（即使 token 有 `write:packages`）。不要尝试用 curl/script 自动切换。

> 提示：已有的 `netmirror-agent:fixed`、`netmirror-panel:fixed` 同样走 ghcr，拉取逻辑一致。

---

## 3. 拉取镜像

```bash
docker pull ghcr.io/asxiaowen/netmirror-gateway:latest
```

---

## 4. 运行 — 面板模式（Panel / 主节点前置登录）

前置条件：本机已运行 NetMirror 面板/后端，监听在 `127.0.0.1:3001`（即 `UPSTREAM` 默认值）。

```bash
mkdir -p /opt/nm-gateway

docker run -d \
  --name nm-gateway \
  --restart always \
  --network host \
  -v /opt/nm-gateway:/data \
  -e PORT=3000 \
  -e UPSTREAM=127.0.0.1:3001 \
  ghcr.io/asxiaowen/netmirror-gateway:latest
```

说明：
- **必须 `--network host`**：① 网关要访问宿主机 `127.0.0.1:3001` 的后端；② 避免访客 IP 被 docker 网桥 NAT 成 `172.17.0.1`（网关已注入 `X-Forwarded-For`/`X-Real-IP` 还原真实访客 IP，配合 host 网络才能生效）。
- `-v /opt/nm-gateway:/data`：持久化登录用户、会话、分享链接、密钥等。该目录里至少应放：
  - `users.txt`（账号密码，格式 `user:password` 每行一条）
  - `admin.key`（控制台入口密码）
  - `panel.key`（面板管理员 API Key，用于节点增删）
  - 其余 `sessions.json` / `shares.json` / `admin_sessions.json` 由网关自动生成。
- 访问 `http://<服务器IP>:3000/login` 即为网关登录页（含**深/浅色切换**按钮，偏好存于浏览器 `localStorage.theme`，登录后 NetMirror SPA 自动继承）。

---

## 5. 运行 — Agent 模式（子节点，无 UI 纯反代）

子节点只做反向代理，不提供登录界面，由面板统一调度：

```bash
mkdir -p /opt/nm-gateway

docker run -d \
  --name nm-gateway-agent \
  --restart always \
  --network host \
  -v /opt/nm-gateway:/data \
  -e PORT=3000 \
  -e UPSTREAM=127.0.0.1:3001 \
  -e AGENT_MODE=true \
  -e ALLOW_IPS=<PANEL_HOST> \
  ghcr.io/asxiaowen/netmirror-gateway:latest
```

- `AGENT_MODE=true`：关闭登录 UI，所有请求透明反代到后端。
- `ALLOW_IPS=<PANEL_HOST>`：仅放行面板 IP（多个用逗号分隔）。**留空则变为公开 agent**（NetMirror 面板 UI 需要浏览器直接访问时才会用）。

---

## 6. 环境变量速查表

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | `3000` | 网关监听端口 |
| `UPSTREAM` | `127.0.0.1:3001` | 后端（面板/agent）地址 `host:port` |
| `AGENT_MODE` | `false` | `true/1/yes` 启用纯反代无 UI 模式 |
| `ALLOW_IPS` | 空 | 仅放行这些源 IP（逗号分隔）；设置后隐式开启 agent 模式 |
| `PEER_IPS` | 空 | 信任的对等节点 IP（面板间互信，跳过鉴权） |
| `USERS_FILE` | `/data/users.txt` | 登录账号文件 |
| `SESS_FILE` | `/data/sessions.json` | 登录会话文件 |
| `SHARES_FILE` | `/data/shares.json` | 分享链接文件 |
| `ADMIN_KEY_FILE` | `/data/admin.key` | 控制台入口密码文件 |
| `PANEL_KEY_FILE` | `/data/panel.key` | 面板管理员 API Key 文件 |
| `PANEL_API_KEY` | 空 | 直接以环境变量传入面板 API Key（代替文件） |
| `CONSOLE_KEY` | 空 | 控制台入口密码（环境变量形式） |
| `SESSION_TTL` | `43200` | 登录会话有效期（秒，默认 12h） |
| `ADMIN_TTL` | `43200` | 控制台会话有效期（秒） |
| `TOOLS_FILE` | `/data/tools.json` | Network Tools 开放/禁止控制配置文件（见第 10 节），传入自定义绝对路径可覆盖默认位置 |

---

## 7. 从源码构建并推送到 ghcr（开发/自更新）

源码结构（本项目 `login/` 目录）：
```
login/
├── Dockerfile      # 基于 python:3-alpine，COPY gateway.py -> /app
└── gateway.py      # 网关主程序（纯标准库）
```

构建：
```bash
docker build -t ghcr.io/asxiaowen/netmirror-gateway:latest login/
```

登录（需要 `write:packages` 权限的 PAT）：
```bash
docker login ghcr.io -u asxiaowen -p <你的PAT>
```

推送：
```bash
docker push ghcr.io/asxiaowen/netmirror-gateway:latest
# 如需带日期标签：
docker tag ghcr.io/asxiaowen/netmirror-gateway:latest ghcr.io/asxiaowen/netmirror-gateway:20260902
docker push ghcr.io/asxiaowen/netmirror-gateway:20260902
```

构建关键点回顾：
- `FROM python:3-alpine`，**没有任何 `pip install`**——`gateway.py` 仅依赖 `http.server`/`socketserver`/`socket` 等标准库。
- `WORKDIR /app` + `COPY gateway.py /app/gateway.py`，`CMD ["python","/app/gateway.py"]`。
- 数据目录用 `VOLUME /data` 暴露，运行时由宿主机挂载。
- 运行端**必须 `--network host`**（原因见第 4 节）。

---

## 8. 升级现有网关（246 / 247 已部署机器）

现有网关以 `python:3-alpine` 容器运行，把宿主机 `/opt/nm-gateway` 挂到 `/data`，且该目录里**已经包含** `gateway.py` + 数据文件。迁移到新镜像只需换镜像、且代码改由镜像 `/app` 提供，数据仍用原目录，**完全无损**：

```bash
# 以 246 为例（先停旧容器，再起新镜像）。数据目录 /opt/nm-gateway 保持不变。
docker rm -f nm-gateway
docker run -d \
  --name nm-gateway --restart always --network host \
  -v /opt/nm-gateway:/data \
  -e PORT=3000 -e UPSTREAM=127.0.0.1:3001 -e PEER_IPS=<AGENT1_HOST> \
  ghcr.io/asxiaowen/netmirror-gateway:latest
```

> 因为新镜像代码在 `/app`、数据在 `/data`，原 `/opt/nm-gateway` 里那个多余的 `gateway.py` 只是被忽略，不影响运行；`users.txt`/`admin.key`/`panel.key` 等数据文件照常被读取。

---

## 9. 验证

1. **登录页与深/浅色切换**：
   ```bash
   curl -s http://<IP>:3000/login | grep -oE '切换主题|nm-theme-toggle|data-theme'
   ```
   应能看到 `切换主题`、`nm-theme-toggle`、`data-theme` 等标记。

2. **真实访客 IP（X-Forwarded-For 修复）**：
   在面板「Your IP Address」处应显示访客公网 IP，而非 `172.17.0.1`。若仍显示网桥 IP，请确认容器是用 `--network host` 启动的。

3. **容器健康**：
   ```bash
   docker logs nm-gateway --tail 20
   # 正常应看到：NM gateway on :3000 -> 127.0.0.1:3001  [PANEL]
   ```

---

## 10. Network Tools 开放/禁止控制

网关支持**按工具粒度**开放或禁止某个 Network Tool，且无需重启（改完文件即时生效）。常见场景：只想给客户开放 Ping/MTR，而把 Shell（交互式终端）、iperf3、Speedtest 关掉。

### 10.1 配置文件 `tools.json`

默认位置 `/data/tools.json`（与网关数据目录一致，即宿主机的 `/opt/nm-gateway/tools.json`）。也可用环境变量 `TOOLS_FILE` 指定自定义绝对路径。文件用 JSON 描述 8 个逻辑开关：

```json
{
  "tools": {
    "ping":           true,
    "mtr":            true,
    "traceroute":     true,
    "iperf3":         true,
    "speedtest_dot_net": true,
    "shell":          true,
    "librespeed":     true,
    "filespeedtest":  true
  }
}
```

8 个开关的含义（**IPv6 共用 IPv4 开关**——禁用 `ping` 会一并禁用 `ping6`，`mtr`/`traceroute` 同理）：

| 开关 | 控制的功能 |
|------|-----------|
| `ping` | Ping + Ping6 |
| `mtr` | MTR + MTR6 |
| `traceroute` | Traceroute + Traceroute6 |
| `iperf3` | iPerf3 测速 |
| `speedtest_dot_net` | Speedtest.net 测速 |
| `shell` | 交互式 Shell（WebSocket） |
| `librespeed` | LibreSpeed 上传/下载测速 |
| `filespeedtest` | 文件下载测速 |

- **缺省即全开**：文件不存在、或某键缺失时，对应工具默认 `true`（开放）。
- **热加载**：网关每次请求时对比文件 `mtime`，改完保存即生效，无需 `docker restart`。
- 把某个值改为 `false` 即禁止该工具。

### 10.2 三处生效点（网关如何阻止）

1. **边缘即时拦截（403）**：当访客直接请求被禁用的工具（如 `GET /method/ping`），网关在反代前就返回 `403`，响应体为
   ```json
   {"success": false, "error": "工具 'ping' 已被管理员禁用"}
   ```
   后端根本收不到这次请求。

2. **前端按钮隐藏（`/session` SSE 改写）**：网关订阅后端的 `/session` SSE 流，解析其中的 `Config` 事件（含 `feature_ping`/`feature_shell`/`feature_iperf3` 等标志），把被禁用工具的 `feature_*` 改写为 `false` 后再转发给浏览器。NetMirror 前端据此自动隐藏对应按钮——用户**连入口都看不到**。

3. **控制台 UI + API**：见 10.3 / 10.4。

> 注意：边缘 403 与 SSE 改写**双重保险**。即使有用户绕过前端直接调用 API，边缘拦截仍会拒绝；而前端隐藏又避免了「按钮点了没反应」的困惑。

### 10.3 控制台 UI（推荐操作方式）

登录网关控制台（面板模式下访问 `http://<IP>:3000/console`，用 `admin.key` 中的密码进入）后，新增 **「Network Tools 开放控制」** 卡片：

- 列出全部 8 个工具的中文名 + 开关（复选框）。
- 取消勾选 = 禁止；勾选 = 开放。
- 点 **「保存设置」** → 立即写入 `/data/tools.json`，全网关切生效（包括正在进行的会话）。

### 10.4 控制台 API（便于自动化 / 脚本）

- **GET `/console/api/tools`**（需 `nm_admin` 控制台会话 Cookie）
  返回：
  ```json
  {
    "tools": [
      {"id": "ping", "label": "Ping", "enabled": true},
      {"id": "mtr",  "label": "MTR",  "enabled": true},
      "...": "其余 6 个"
    ],
    "file": "/data/tools.json"
  }
  ```
- **PUT `/console/api/tools`**（同上鉴权），请求体：
  ```json
  {"tools": {"ping": false, "shell": false}}
  ```
  只传需要变更的键即可；网关读回旧文件、合并覆盖后原子写入（`tmp` + `os.replace`），写完即时生效。

### 10.5 典型示例

只开放 Ping 与 MTR，其余全禁（`/data/tools.json`）：
```json
{
  "tools": {
    "ping":           true,
    "mtr":            true,
    "traceroute":     false,
    "iperf3":         false,
    "speedtest_dot_net": false,
    "shell":          false,
    "librespeed":     false,
    "filespeedtest":  false
  }
}
```
保存后：访客在 NetMirror 界面只能看到 Ping / MTR 两个入口；直接 `GET /method/shell` 会被网关 `403` 挡回。

---

## 11. 常见问题

- **`python: can't open file '/data/gateway.py'`**：你跑的是旧版镜像（代码曾放在 `/data`）。请 `docker pull` 最新 `latest` 后再运行；新版代码在 `/app`。
- **`unauthorized` 拉取失败**：ghcr 包是 private，先按第 2 节登录，或把包设为 Public。
- **面板显示 IP 为 `172.17.0.1`**：容器未用 `--network host`，或后端未信任 XFF。本镜像已注入 `X-Forwarded-For`，只要 host 网络即可还原真实 IP。
- **Agent 模式下面板连不上**：检查 `ALLOW_IPS` 是否包含面板 IP；公开 agent 则保持 `ALLOW_IPS` 为空。
- **MTR / Ping 等测试报 502**：`/method/<tool>` 是同步长轮询，后端等工具跑完才返回响应头。旧版网关读取响应头超时仅 15s，而 `mtr --report-cycles 10` 对远端目标通常需要 10~16s，刚好踩线超时。`latest` / `20260902` 已把 `/method/*` 路径的读头超时提升到 75s，可覆盖后端 60s 工具超时。若仍用旧镜像，请按第 8 节升级。
- **Shell（交互式终端）打不开 / 升级后提示 `HTTP/1.1 400 Bad Request`**：Shell 走的是 **WebSocket**（`/session/<sid>/shell` 升级），而默认 HTTP 反代逻辑只能处理普通请求、无法隧道化 WS 升级握手。`latest` / `20260902` 已新增原生 WebSocket 隧道：网关与后端分别建立 socket，原样转发 `Upgrade`/`Sec-WebSocket-Key` 等头并双向透传字节流，Shell 现已可正常使用（验证返回 `101 Switching Protocols`）。若务必用旧镜像，请按第 8 节升级。
- **248（原生 systemd 部署）如何更新网关代码**：248 的网关以宿主机 `systemd` 的 `nm-gateway.service` 运行（代码在 `/opt/nm-gateway/gateway.py`，非容器）。更新方式与 246/247 的容器升级不同——用 `update_248_gateway.py`（本项目脚本）把本地 `login/gateway.py` SFTP 推到 248 的 `/opt/nm-gateway/gateway.py`（自动备份 `.bak`），再 `systemctl restart nm-gateway` 即生效。
