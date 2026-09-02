# NetMirror 新机器部署教程（双面板互管）

> 适用场景：给一台新机器装上 NetMirror，并和已有的另一台组成「每台一个 panel、互相注册」的双向互管拓扑。
> 镜像已公开：`ghcr.io/asxiaowen/netmirror-panel:fixed` 与 `ghcr.io/asxiaowen/netmirror-agent:fixed`，**匿名可直接 pull，无需 docker login**（已实测匿名 manifest 返回 200）。

---

## 一、部署目标

- 每台机器都跑一个 `netmirror-panel`：自带 Web UI + 完整测速后端，同时自身也能当被调度的节点。
- 两台互相把对方注册为自己的节点 ⇒ 任意一台面板都能调度两台机器测速。
- 三台及以上时，建议只新增 `agent` 节点（见第六节），不必每台都开 panel。

```
        ┌─────────────┐        注册对端        ┌─────────────┐
        │  Panel A    │ ───────────────────▶ │  Panel B    │
        │ (246:3000)  │ ◀─────────────────── │ (247:3000)  │
        └──────┬──────┘   互相注册 / 互调测速   └──────┬──────┘
               │ 节点: A(self) + B(peer)             │ 节点: B(self) + A(peer)
               └─────────────────────────────────────┘
```

---

## 二、前提条件（新机器）

- 已安装 Docker（CentOS 7 / Ubuntu / Debian 均可）。
- 机器能出网访问 `ghcr.io`。
- 防火墙放行 `3000/tcp`（脚本会自动放，手动也要放）。
- 两台机器之间 `3000` 端口互通（面板之间要互相注册、互相下发测速任务）。
- **多台机器的 `ADMIN_API_KEY` 必须完全一致**，否则节点间 API 调用会被拒。

---

## 三、方式一：一键脚本（推荐）

工作区里已有 `deploy-panel.sh`，把它传到新机器（scp 或复制文件内容 vim 粘贴），然后在两台机器各跑一次、参数互换：

```bash
chmod +x deploy-panel.sh

# 机器 A（示例 246）
./deploy-panel.sh <PANEL_HOST> <AGENT1_HOST> <ADMIN_API_KEY>

# 机器 B（示例 247）—— 参数顺序对调
./deploy-panel.sh <AGENT1_HOST> <PANEL_HOST> <ADMIN_API_KEY>
```

脚本自动完成：防火墙放行 3000 → 匿名 pull 镜像 → 以 host 网络起 panel 容器 → 在本机面板注册自己 + 注册对端 → 打印结果。

参数说明：`deploy-panel.sh <本机IP> <对端IP> <ADMIN_KEY> [GHCR_TOKEN]`
- 镜像已公开，`GHCR_TOKEN` 可省略；
- 若将来镜像又变私有，把 Classic PAT 作为第 4 参数传入，脚本会自动 `docker login ghcr.io -u asxiaowen`。

---

## 四、方式二：纯手动（不用脚本，零文件传输）

### 1) 起 panel 容器（每台改 IP / LOCATION 即可）

```bash
docker rm -f netmirror-panel 2>/dev/null
docker run -d --name netmirror-panel --restart always --network host \
  -e HTTP_PORT=3000 \
  -e ADMIN_API_KEY='<ADMIN_API_KEY>' \
  -e LOCATION='HKG1-NewNode' \
  -v /opt/netmirror/data:/data \
  ghcr.io/asxiaowen/netmirror-panel:fixed
```

### 2) 等面板就绪

```bash
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:3000/ >/dev/null && break; sleep 2; done
```

### 3) 注册自己进本机面板

```bash
TOK=$(curl -s -X POST http://127.0.0.1:3000/api/admin/tokens \
  -H "X-API-Key: <ADMIN_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name":"self","location":"HKG1-本机IP","expires_in":0}' | grep -o '"token":"[^"]*"' | sed 's/.*:"//;s/"//')
curl -X POST http://127.0.0.1:3000/api/register \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOK\",\"url\":\"http://本机IP:3000\"}"
```

### 4) 注册对端进本机面板

```bash
TOK=$(curl -s -X POST http://127.0.0.1:3000/api/admin/tokens \
  -H "X-API-Key: <ADMIN_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name":"peer","location":"HKG1-对端IP","expires_in":0}' | grep -o '"token":"[^"]*"' | sed 's/.*:"//;s/"//')
curl -X POST http://127.0.0.1:3000/api/register \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOK\",\"url\":\"http://对端IP:3000\"}"
```

> NetMirror 2025-12-23 重构后节点列表纯 API 管理，面板**不会**自动把自己加为节点，所以「注册自己」这步不能省。

---

## 五、验证

- 浏览器打开 `http://本机IP:3000` → 右下角设置 → 节点列表，应看到 `self-本机IP` 和 `对端IP` 两个节点，状态 online。
- 选任一节点跑一次 Ping / MTR / Speedtest / LibreSpeed，确认能正常出数（LibreSpeed 上传已修，不会再恒 0）。

---

## 六、再加第三台（只当节点，不开面板）

新机器跑 `agent` 镜像，再在 246 / 247 任一面板里把它注册为节点：

```bash
docker run -d --name netmirror-agent --restart always --network host \
  -e AGENT_MODE=true -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-Node3' -e PUBLIC_IPV4=第三台IP \
  ghcr.io/asxiaowen/netmirror-agent:fixed
```

然后在已有 panel 的管理界面用「Deploy Tokens」生成脚本，或手动 `/api/register` 把第三台加进去（token 从对应 panel 的 `/api/admin/tokens` 取）。

---

## 六之二、批量添加多台（中心面板 + agent，推荐）

节点超过 2~3 台时，**建议固定一台 panel 作「中心」，其余全跑 agent（星型拓扑）**，而不是 N 台全开 panel 互连（那会是 N×N 的注册量，难维护）。新增 N 台只需两步：

### 1) 每台新机器启动 agent（一行，IP 改成自己的）

```bash
docker run -d --name netmirror-agent --restart always --network host \
  -e AGENT_MODE=true -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-NodeX' -e PUBLIC_IPV4=本机IP \
  ghcr.io/asxiaowen/netmirror-agent:fixed
```

> 若已配好免密 SSH，可在中心机上一条循环批量下发：
> ```bash
> for ip in 1.2.3.4 1.2.3.5 1.2.3.6; do
>   ssh root@$ip "docker run -d --name netmirror-agent --restart always --network host \
>     -e AGENT_MODE=true -e HTTP_PORT=3000 -e LOCATION=HKG1-\$ip -e PUBLIC_IPV4=\$ip \
>     ghcr.io/asxiaowen/netmirror-agent:fixed"
> done
> ```

### 2) 在中心面板一侧，用脚本一次性注册多台

```bash
./add-nodes.sh 中心面板IP <ADMIN_API_KEY> 1.2.3.4 1.2.3.5 1.2.3.6
```

脚本对每个 IP 自动建 deploy token 并 `POST /api/register`，逐个回报成功/失败。

> - 中心面板自身也能当节点：在中心机跑一次 `./deploy-panel.sh 中心IP none KEY`（peer 填 `none`）即可把自己注册进去。
> - 官方替代方案：中心面板 UI → 设置 → Deploy Tokens 生成一条 curl 命令，粘贴到目标机执行即可自动装 Docker + 起 agent + 注册，适合给客户/同事自助加节点。

这样 N 台机器 = 1 次 agent 下发（可并行）+ 1 条 `add-nodes` 命令，远比「每台全 panel 互连」好维护。

---

## 六之三、裸机新机器（未装 Docker）一键加入

如果新机器是干净的、**连 Docker 都还没装**，用 `bootstrap-agent.sh` 一条命令搞定（装 Docker → 起 agent → 到中心面板注册自己）：

```bash
./bootstrap-agent.sh 中心面板IP 本机IP <ADMIN_API_KEY>
```

脚本自动：检测并安装 Docker（官方 `get.docker.com` 一键脚本，支持 CentOS7/Ubuntu/Debian）→ 放行 3000 → 起 `netmirror-agent` 容器 → 向中心面板 `POST /api/register` 把本机注册为节点。

> 前提：新机器能出网拉 `ghcr.io` 镜像，且能访问中心面板 `:3000`（中心面板也要能回连本机 `:3000`）。
> 若更想让这台也做独立 panel（而非 agent 节点），先手动装好 Docker，再用 `deploy-panel.sh 本机IP 中心IP KEY`（peer 填中心面板 IP，让它和中心互管）。

---

## 七、排障速查

| 现象 | 原因 / 处理 |
|------|------|
| 拉镜像慢 / 超时 | 确认能访问 ghcr.io；国内机器可配镜像加速 |
| 节点显示 offline | 两台 `3000` 互不通；或 `ADMIN_API_KEY` 不一致 |
| 注册返回 401 | `ADMIN_API_KEY` 错 |
| 注册返回 403 / 404 | 面板没起来或端口不对，先看 `docker logs netmirror-panel` |
| CentOS 7 起容器报 `/run` 权限 | 与我们 panel 镜像无关（那是 `nginx:alpine` 的老内核坑）；如遇可换 host 网络 |

---

## 八、相关文件 / 资源

- `deploy-panel.sh`：本工作区的一键部署脚本
- Panel 镜像：`ghcr.io/asxiaowen/netmirror-panel:fixed`（已公开）
- Agent 镜像：`ghcr.io/asxiaowen/netmirror-agent:fixed`（已公开）
- 历史修复：`NetMirror-CORS-修复教程.md`、`netmirror-cors-fix.patch`（LibreSpeed 上传 CORS 修复）
