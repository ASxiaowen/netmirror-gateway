# NetMirror 主控 / 被控 搭建教程（Panel + Agent）

> 适用：从零部署一套 NetMirror 网络测速平台。
> **主控 = Panel**（带 Web UI + 完整测速后端，能调度所有节点）；**被控 = Agent**（只跑测速后端，被主控调度，无 UI）。
> 镜像已公开（`ghcr.io/asxiaowen/netmirror-panel:fixed` / `netmirror-agent:fixed`），匿名可直接 pull。
> 配套一键脚本见仓库 `scripts/`：`deploy-panel.sh`、`bootstrap-agent.sh`、`add-nodes.sh`。

---

## 1. 架构与拓扑

推荐 **「1 个主控 + N 个被控」星形拓扑**：主控是唯一面板，所有被控都是它的节点。

```
            ┌─────────────────────────────┐
            │        主控 Panel           │
            │  <PANEL_HOST>:3000 (Web UI) │
            └──────────┬─────────┬────────┘
        注册/调度      │         │   注册/调度
                       ▼         ▼
              ┌────────────┐  ┌────────────┐
              │ 被控 Agent │  │ 被控 Agent │   ... 可横向扩 N 台
              │ <AGENT1>  │  │ <AGENT2>  │
              └────────────┘  └────────────┘
```

- **主控（Panel）**：自带 Web 界面，能看见/调度所有节点，也能把自己当作一个测速节点。
- **被控（Agent）**：纯后端，无 UI，由主控通过 API 调度。
- 多台全开 Panel 互连 = N×N 注册量，难维护；**超过 2~3 台请固定一台做主控、其余全做 Agent**。
- （可选）在每台前面再套一层 `netmirror-gateway` 做登录/访问控制，见第 6 节与 `NetMirror-Gateway-Docker教程.md`。

---

## 2. 前置条件（每台机器）

| 项 | 要求 |
|----|------|
| 系统 | 已装 Docker（CentOS 7 / Ubuntu / Debian 通用） |
| 出网 | 能访问 `ghcr.io`（拉镜像） |
| 端口 | 放行 `3000/tcp`（测速后端与面板 UI 共用） |
| 互通 | 主控能访问被控 `:3000`，**且被控也能回连主控 `:3000`**（否则节点 offline） |
| 密钥 | 所有 Panel 的 `ADMIN_API_KEY` **必须完全一致**（节点间 API 调用用 `X-API-Key` 头鉴权） |

> 一台机器上若还没有 Docker，直接用第 4/5 节的一键脚本即可（脚本会自动装 Docker）。

---

## 3. 准备变量

把下面这些换成你自己的，后文命令直接替换即可：

```bash
PANEL_HOST="<主控IP>"          # 主控公网/内网 IP
AGENT_HOST="<被控IP>"          # 被控 IP
ADMIN_API_KEY="<ADMIN_API_KEY>" # 自己随便定一个强随机串，所有面板保持一致
```

---

## 4. 部署主控（Panel）

### 方式 A：一键脚本（推荐）

把仓库里的 `scripts/deploy-panel.sh` 传到机器上执行：

```bash
chmod +x deploy-panel.sh
# 用法: deploy-panel.sh <本机IP> <对端PanelIP或none> <ADMIN_API_KEY>
./deploy-panel.sh "$PANEL_HOST" none "$ADMIN_API_KEY"
```

脚本自动完成：放行 3000 → 匿名 pull 镜像 → `--network host` 起 panel 容器 → 在本机面板注册自己 → 打印结果。
（若将来镜像改私有，可把 GitHub Classic PAT 作为第 4 个参数传入，脚本会自行 `docker login ghcr.io`。）

### 方式 B：纯手动

```bash
docker rm -f netmirror-panel 2>/dev/null
docker run -d --name netmirror-panel --restart always --network host \
  -e HTTP_PORT=3000 \
  -e ADMIN_API_KEY="$ADMIN_API_KEY" \
  -e LOCATION='HKG1-Panel' \
  -v /opt/netmirror/data:/data \
  ghcr.io/asxiaowen/netmirror-panel:fixed

# 等面板就绪
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:3000/ >/dev/null && break; sleep 2; done
```

> **必须 `--network host`**：测速后端要直接收发原始网络包，bridge 网络会丢包/测不准。
> **别漏「注册自己」**：2025-12-23 重构后节点列表纯 API 管理，面板**不会**自动把自己加为节点。

注册自己进本机面板：

```bash
TOK=$(curl -s -X POST http://127.0.0.1:3000/api/admin/tokens \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"self","location":"HKG1-Panel","expires_in":0}' \
  | grep -o '"token":"[^"]*"' | sed 's/.*:"//;s/"//')

curl -X POST http://127.0.0.1:3000/api/register \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOK\",\"url\":\"http://$PANEL_HOST:3000\"}"
# 期望: {"message":"Node registered successfully",...}
```

---

## 5. 部署被控（Agent）并接入主控

### 方式 A：裸机一键脚本（推荐，连 Docker 都没装也行）

把 `scripts/bootstrap-agent.sh` 传到被控机器执行：

```bash
chmod +x bootstrap-agent.sh
# 用法: bootstrap-agent.sh <主控IP> <本机IP> <ADMIN_API_KEY>
./bootstrap-agent.sh "$PANEL_HOST" "$AGENT_HOST" "$ADMIN_API_KEY"
```

脚本自动：检测/安装 Docker → 放行 3000 → 起 `netmirror-agent` 容器 → 向主控 `POST /api/register` 把本机注册为节点。
想同时挂到第二个主控，把第一个参数换成另一台主控 IP 再跑一次即可。

### 方式 B：已装 Docker，手动起 Agent

```bash
docker run -d --name netmirror-agent --restart always --network host \
  -e AGENT_MODE=true -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-Node' -e PUBLIC_IPV4="$AGENT_HOST" \
  ghcr.io/asxiaowen/netmirror-agent:fixed
```

然后在**主控**侧注册这台被控（token 从主控 `/api/admin/tokens` 取）：

```bash
# 在主控上执行
TOK=$(curl -s -X POST http://"$PANEL_HOST":3000/api/admin/tokens \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"node","location":"HKG1-Node","expires_in":0}' \
  | grep -o '"token":"[^"]*"' | sed 's/.*:"//;s/"//')

curl -X POST http://"$PANEL_HOST":3000/api/register \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOK\",\"url\":\"http://$AGENT_HOST:3000\"}"
```

### 批量加多台

在**主控**上用仓库脚本一次性注册多台已起 Agent 的机器：

```bash
./add-nodes.sh "$PANEL_HOST" "$ADMIN_API_KEY" 1.2.3.4 1.2.3.5 1.2.3.6
```

### 关键 API 契约（务必照此，否则踩坑）

| 端点 | 要点 |
|------|------|
| `POST /api/admin/tokens` | 鉴权头必须是 **`X-API-Key`**（不是 `X-Admin-API-Key`）；body **必须含 `location` 字段**，否则 400 |
| `POST /api/register` | body `{"token":"<上面的token>","url":"http://<被控IP>:3000"}` |
| `GET /api/admin/tokens` | 列出 token（含 `id`/`name`/`status`） |
| `DELETE /api/admin/tokens/<id>` | 删除 token（**不影响**已注册节点） |
| 节点列表 | 没有 `/api/nodes` 端点，看状态请到面板 UI：设置 → 节点列表 |

---

## 6. （可选）在主控前加网关做登录/访问控制

如果不希望面板直接暴露公网，可在主控前套一层 `netmirror-gateway`：

- **面板模式（主控前）**：`docker run ... netmirror-gateway:latest`，带 `users.txt`（登录账号）、`admin.key`（控制台密码），提供登录页 + 访问控制。
- **Agent 模式（被控前）**：`AGENT_MODE=true` + `ALLOW_IPS=<主控IP>`，仅放行主控调度，无 UI。

详见仓库 `docs/NetMirror-Gateway-Docker教程.md`。网关还支持 **Network Tools 逐项开放/禁止控制**（控制台 UI + `/console/api/tools`），可按需关掉 Shell / iPerf3 / Speedtest 等。

---

## 7. 验证（部署后必做）

| 验证项 | 命令 / 位置 | 期望 |
|--------|------------|------|
| 主控存活 | `curl -fsS http://$PANEL_HOST:3000/ -o /dev/null -w '%{http_code}'` | `200` |
| 被控存活 | 在被控上 `curl -fsS http://127.0.0.1:3000/ -o /dev/null -w '%{http_code}'` | `200` |
| 主控能回连被控 | 在主控上 `curl http://$AGENT_HOST:3000/ -o /dev/null -w '%{http_code}'` | `200`（否则节点 offline） |
| UI 节点状态 | 主控 → 设置 → 节点列表 | 主控(self) 与被控都 `online` |
| 功能测速 | 选节点跑 Ping / MTR / Speedtest | 正常出数、无超时 |
| API 自测 | `python3 client/nm_api_client.py http://$AGENT_HOST:3000 8.8.8.8 ping` | 打印 SessionId + 测速结果 |

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|------|------------|
| 拉镜像慢 / 超时 | 确认能访问 `ghcr.io`；国内机器可配镜像加速 |
| 节点 offline | 主控↔被控 `3000` 互不通；或被控起容器没用 `--network host` |
| 注册返回 401 | `ADMIN_API_KEY` 不匹配（主控与被控必须一致） |
| 注册返回 403/404 | 面板没起来或端口不对，先 `docker logs netmirror-panel` |
| 测速全 0 / 不准 | 容器没用 `--network host`；或防火墙挡了测速端口 |
| 想给客户自助加节点 | 主控 UI → 设置 → Deploy Tokens 生成一条 curl，粘贴到目标机执行即自动装 Docker+起 Agent+注册 |

---

## 9. 相关文件

| 文件 | 用途 |
|------|------|
| `scripts/deploy-panel.sh` | 一键部署主控（Panel） |
| `scripts/bootstrap-agent.sh` | 裸机一键加入被控（装 Docker + 起 Agent + 注册） |
| `scripts/add-nodes.sh` | 批量把多台 Agent 注册进主控 |
| `scripts/nm_deploy_panel.py` / `nm_deploy_agent.py` / `nm_register.py` | 对应 Python 实现，便于二次开发 |
| `client/nm_api_client.py` | NetMirror API 客户端（SSE 会话 + 下发测速），可用于自动化验证 |
| `docs/NetMirror-Gateway-Docker教程.md` | 网关（登录/访问控制）部署 |
| `docs/NetMirror-测速API教程.md` | Speedtest / iPerf3 程序化调用 |
