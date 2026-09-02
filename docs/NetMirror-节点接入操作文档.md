# NetMirror 节点接入操作文档

> 适用：把一台新机器接进已有的 NetMirror 面板（中心面板 + agent 星型拓扑）。
> 本文以 **<AGENT2_HOST>** 实际接入 **246 / 247** 双面板为范例，命令均已实测可用。
> 镜像已公开（`ghcr.io/asxiaowen/netmirror-agent:fixed` / `netmirror-panel:fixed`），无需 docker login。

---

## 一、拓扑与角色

```
         ┌──────────────┐         ┌──────────────┐
         │ Panel 246    │ ◀──互管──▶ │ Panel 247    │
         │ <PANEL_HOST>│         │ <AGENT1_HOST>│
         └──────┬───────┘         └──────┬───────┘
                │ 注册节点                 │ 注册节点
                ▼                         ▼
         ┌──────────────┐
         │ Agent 248    │   (新接入节点，只当被调度节点)
         │ <AGENT2_HOST>│
         └──────────────┘
```

- **中心面板**：246 / 247 各自是 panel，且互相注册（双面板互管）。
- **新节点**：建议以 `agent` 形式挂到中心面板（星型），不要每台都开 panel（否则是 N×N 注册量，难维护）。
- 新机器需要 Docker（脚本会自动装）。

---

## 二、前置条件

| 项 | 要求 |
|----|------|
| 出网 | 新机能拉 `ghcr.io`（镜像已公开） |
| 端口 | 新机放行 `3000/tcp`（脚本自动放） |
| 互通 | 新机能访问中心面板 `:3000`，且**中心面板能回连新机 `:3000`**（否则节点 offline） |
| 密钥 | 所有面板 `ADMIN_API_KEY` 必须一致（面板 env 名 `ADMIN_API_KEY`，API 调用用 `X-API-Key` 头传递，见第四节） |

> ⚠️ 密码提示：248 的 root 密码是 `<ROOT_PASSWORD>`，与 246/247 的 `<ROOT_PASSWORD>` **不同**，SSH 时注意别用错。

---

## 三、接入步骤

### 方式 A：一键脚本（推荐）

把 `bootstrap-agent.sh` 传到新机器，一行搞定（装 Docker → 起 agent → 注册到中心面板）：

```bash
./bootstrap-agent.sh <PANEL_HOST> <AGENT2_HOST> <ADMIN_API_KEY>
# 参数: <中心面板IP> <本机IP> <ADMIN_API_KEY>
```

要同时挂到第二个面板（247），再跑一次、把中心面板 IP 换成 247：

```bash
./bootstrap-agent.sh <AGENT1_HOST> <AGENT2_HOST> <ADMIN_API_KEY>
```

脚本输出最后应看到 `✓ 本机已注册到中心面板 <PANEL_HOST>` 之类成功提示。

### 方式 B：纯手动（不用脚本，便于理解 API）

**1) 装 Docker（CentOS7 / Ubuntu / Debian 通用）**
```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

**2) 起 agent 容器**
```bash
docker run -d --name netmirror-agent --restart always --network host \
  -e AGENT_MODE=true -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-<AGENT2_HOST>' -e PUBLIC_IPV4=<AGENT2_HOST> \
  ghcr.io/asxiaowen/netmirror-agent:fixed
```

**3) 建 deploy token（⚠️ 见第四节契约）**
```bash
TOK=$(curl -s -X POST http://<PANEL_HOST>:3000/api/admin/tokens \
  -H "X-API-Key: <ADMIN_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name":"node-248","location":"HKG1-<AGENT2_HOST>","expires_in":0}' \
  | grep -o '"token":"[^"]*"' | sed 's/.*:"//;s/"//')
```

**4) 注册本机进中心面板**
```bash
curl -X POST http://<PANEL_HOST>:3000/api/register \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOK\",\"url\":\"http://<AGENT2_HOST>:3000\"}"
# 期望返回: {"message":"Node registered successfully",...}
```

---

## 四、关键 API 契约（踩坑记录，务必照此）

> 接入时连续踩了两个坑，已修进所有脚本与本文，照抄即可避坑。

### 1. admin token 端点 `POST /api/admin/tokens`
- **鉴权头必须用 `X-API-Key`**，不是 `X-Admin-API-Key`（后者返回 `{"error":"Invalid API key"}`）。
- **body 必须含 `location` 字段**，否则 400 报 `Location failed on the 'required' tag`。
- 正确示例：
  ```bash
  curl -X POST http://<面板IP>:3000/api/admin/tokens \
    -H "X-API-Key: <ADMIN_API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"name":"x","location":"HKG1-<IP>","expires_in":0}'
  ```
- 返回：`{"success":true,"token":{"id":"token_xxx","token":"<明文token>",...}}`

### 2. 注册端点 `POST /api/register`
- body：`{"token":"<上面拿到的token>","url":"http://<新机IP>:3000"}`
- 返回：`{"message":"Node registered successfully","node":{...},"success":true}`

### 3. token 管理
- 列出：`GET /api/admin/tokens`（带 `X-API-Key` 头）→ 含 `id` / `name` / `status`
- 删除：`DELETE /api/admin/tokens/<id>` → `{"message":"Token deleted successfully"}`
  （删 token **不影响**已注册节点）

### 4. 节点列表
- **没有** `/api/nodes` 端点（返回 404）。看节点状态请到面板 UI：设置 → 节点列表。

---

## 五、验证（接入后必做）

| 验证项 | 命令 / 位置 | 期望 |
|--------|------------|------|
| 本机 agent 存活 | `curl -fsS http://127.0.0.1:3000/ -o /dev/null -w '%{http_code}'` | `200` |
| 面板侧能回连 | 在中心面板机上 `curl http://<新机IP>:3000/ -o /dev/null -w '%{http_code}'` | `200`（否则节点会 offline） |
| 功能测速 | 面板 UI 选该节点跑 Ping/Speedtest；或用 `nm_api_client.NetMirror("http://<新机IP>:3000").run("ping","8.8.8.8")` | 有正常回包、无超时 |
| UI 状态 | 面板 → 设置 → 节点列表 | 该节点显示 `online` |

> 248 实测：本机 Ping 8.8.8.8 返回 10 个正常结果；246/247 主机侧回连 248:3000 均 200 → 两面板均 online。

---

## 六、清理（可选）

调试产生的临时 deploy token 建议删掉（避免留下可注册凭证）：
```bash
# 先列出来找到目标 id
curl -s -H "X-API-Key: <ADMIN_API_KEY>" http://<面板IP>:3000/api/admin/tokens
# 再删
curl -X DELETE -H "X-API-Key: <ADMIN_API_KEY>" http://<面板IP>:3000/api/admin/tokens/<id>
```

---

## 七、248 实际接入记录（范例）

- **时间**：2026-08-31
- **目标**：<AGENT2_HOST>（裸机，原无 Docker）
- **过程**：
  1. SSH 登录 248（root / `<ROOT_PASSWORD>`）。
  2. `bootstrap-agent.sh <PANEL_HOST> <AGENT2_HOST> <KEY>` → 自动装 Docker 29.7.2 + 起 `netmirror-agent:fixed`（LOCATION=HKG1-<AGENT2_HOST>）。
  3. 首跑注册失败（rc=1）→ 排查发现脚本用了错误请求头 `X-Admin-API-Key` 且 token body 缺 `location` → 修正为 `X-API-Key` + 带 `location`。
  4. 修正后重新注册：246、247 均返回 `Node registered successfully`。
  5. 端到端验证：248 自跑 Ping 8.8.8.8 正常；246/247 回连 248:3000 均 200。
  6. 清理调试 token（`diag2`）。
- **结果**：248 作为 agent 节点挂入 246 与 247 双面板，状态 online。

---

## 八、相关文件

| 文件 | 用途 |
|------|------|
| `bootstrap-agent.sh` | 裸机一键接入（装 Docker+起 agent+注册） |
| `deploy-panel.sh` | 把一台机器部署为 panel 并互管 |
| `add-nodes.sh` | 批量把多台 agent 注册进中心面板 |
| `nm_api_client.py` | NetMirror API 客户端（SSE 会话 + 下发测量），可用于自动化验证 |
| `test_248.py` / `check_reach.py` | 248 接入后的功能 / 回连验证脚本 |
| `NetMirror-新机器部署教程.md` | 更完整的部署教程 |
