# NetMirror 测速 API 教程（Speedtest / iPerf3 程序化调用）

> 适用：NetMirror 面板（Panel）或子节点（Agent）的 Network Tools 接口
> 已对照线上实例验证（Panel <PANEL_HOST>，Agent <AGENT1_HOST>）
> 配套客户端：`nm_api_client.py`（本仓库 `NetMirror` 类）

---

## 1. API 模型一句话说明

NetMirror 的工具接口是 **「SSE 长连接 + 触发式」** 模型，不是普通的请求-响应：

1. 打开一条 **SSE 长连接** `GET <base>/session`，首条事件 `SessionId` 给出会话令牌。
2. **保持这条连接不关闭**——所有测速结果都通过它流式返回。
3. 另发一条 `GET <base>/method/<工具>?ip=<目标>`（带 `session` 头）去**触发**测试，HTTP 响应本身只是 `{"success":true}`，真正的输出走第 1 步的 SSE 通道。
4. 按 **事件名（event name）** 解析 SSE 数据。

---

## 2. 鉴权说明（直连 vs 经网关）

| 访问方式 | 地址示例 | 是否需要登录 |
|----------|----------|--------------|
| **直连后端 / Agent（IP 白名单放行）** | `http://<AGENT1_HOST>:3000`（Agent 模式无 UI） | 否（靠 `ALLOW_IPS` 白名单） |
| **经网关 Panel 模式** | `http://<PANEL_HOST>:3000`（网关端口） | **是**，需先 `POST /login` 拿到 `nm_sess` Cookie 并随请求带上 |

> 如果你只是想脚本化测速，最省事的是直连 **Agent 节点**（已配置 `ALLOW_IPS` 放行你的调度机 IP），跳过登录。若必须走 Panel 网关，先做一次登录换取 Cookie（见第 7 节）。

---

## 3. 三步调用流程

```
①  GET  <base>/session          → 取首事件 SessionId（同时开启结果流）
②  保持该 SSE 连接打开
③  GET  <base>/method/<tool>?ip=<目标>   Header: session: <SessionId>
       → HTTP 回 {"success":true}
       → 真实结果以事件名 = <tool> 经 ① 的 SSE 流回传
```

关键约束：
- 触发请求里目标参数名必须是 **`ip`**（不是 `host`）。
- `session` 头的值是 **`SessionId`** 事件的 `data` 字段（纯字符串，不要加引号/括号）。
- 触发后**必须继续监听 ① 的 SSE**，否则拿不到结果。

---

## 4. Speedtest（speedtestdotnet）

| 项 | 值 |
|----|----|
| 触发方法（URL 路径） | `/method/speedtestdotnet` |
| SSE 事件名 | `SpeedtestStream` |
| 目标参数 | `ip`（填被测节点自身公网 IP 即可，Speedtest 会自动选最近节点） |
| 典型耗时 | 15~30s，监听窗口建议 ≥ 25s |

示例：对面板自身节点做 Speedtest
```
GET /method/speedtestdotnet?ip=<PANEL_HOST>
Header: session: <SessionId>
```
结果在 `SpeedtestStream` 事件里，字段含下载/上传速率、延迟、抖动、服务器名等（JSON）。

---

## 5. iPerf3

| 项 | 值 |
|----|----|
| 触发方法（URL 路径） | `/method/iperf3` |
| SSE 事件名 | `Iperf3` |
| 目标参数 | `ip` = **iPerf3 服务端 IP**（需先在目标机起 `iperf3 -s`） |
| 依赖 | 目标机已运行 iPerf3 server（`iperf3 -s -p 5201`） |

步骤：
1. 在「被测/对端」机器上启动服务端：`iperf3 -s -p 5201`。
2. 触发：
   ```
   GET /method/iperf3?ip=<iperf3_server_ip>
   Header: session: <SessionId>
   ```
3. 监听 `Iperf3` 事件收集吞吐结果。

> 若返回 `SpeedtestStream` 而非 `Iperf3`，说明你误用了 `speedtestdotnet` 方法名；两者事件名不同。

---

## 6. 事件名速查表（已验证）

| 工具 | 触发方法（/method/...） | SSE 事件名 | 备注 |
|------|------------------------|------------|------|
| Ping | `ping` | `Ping` | IPv6：`ping6` |
| Traceroute | `traceroute` | `TracerouteOutput` | IPv6：`traceroute6` |
| MTR | `mtr` | `MTROutput` | IPv6：`mtr6` |
| iPerf3 | `iperf3` | `Iperf3` | 需对端起 server |
| Speedtest | `speedtestdotnet` | `SpeedtestStream` | **注意不是** `SpeedtestDotNet` |
| Shell | `shell` | （WebSocket，非 SSE） | 交互式终端，走 WS 升级 |

> 网关控制面里的功能开关键名见 `tools.json`（如 `speedtest_dot_net`、`iperf3`），与「触发方法名」是两回事，别混。

---

## 7. Python 示例（推荐，直接用 SDK）

```python
from nm_api_client import NetMirror

# 直连 Agent（无需登录）；经 Panel 网关则需先登录拿 cookie 再传 base
node = NetMirror("http://<AGENT1_HOST>:3000")
print("Session:", node.session_id)
print("Node   :", node.config.get("location"), node.config.get("public_ipv4"))

# Speedtest
ack, results = node.run("speedtestdotnet", "<AGENT1_HOST>", window=25)
print("ACK:", ack)
for r in results:
    print("Speedtest:", r)

# iPerf3（对端需先 iperf3 -s）
ack, results = node.run("iperf3", "对端IP", window=15)
for r in results:
    print("iPerf3:", r)

node.close()
```

`node.run(method, target, window=N)` 内部已封装：先快照事件游标 → 发触发请求 → 监听 `window` 秒收集同名事件。

### 经网关 Panel 模式（带登录）

```python
import urllib.request, http.cookiejar, json

BASE = "http://<PANEL_HOST>:3000"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# 1) 登录拿 nm_sess
login = urllib.request.Request(BASE + "/login",
    data=urllib.parse.urlencode({"user":"<账号>","pass":"<密码>"}).encode(),
    headers={"Content-Type":"application/x-www-form-urlencoded"})
op.open(login, timeout=10)

# 2) 之后所有 /session、/method 请求都复用 op（自动带 cookie）
```

---

## 8. 纯 curl 最小示例（无 SDK）

开两个终端：

```bash
# 终端 A：保持 SSE 连接，取 SessionId 并监听 SpeedtestStream
curl -N "http://<AGENT1_HOST>:3000/session"

# 终端 B：触发（把 <SessionId> 换成 A 输出的纯字符串）
curl -H "session: <SessionId>" \
     "http://<AGENT1_HOST>:3000/method/speedtestdotnet?ip=<AGENT1_HOST>"
```

结果在终端 A 的 SSE 流中以 `event:SpeedtestStream` 出现。

---

## 9. 常见问题

- **触发后 `method` 返回空白 / 无结果**：最常见是忘了「保持 ① 的 SSE 连接」。触发请求和结果流是两条独立的通道，结果只走 SSE。
- **事件名对不上**：Speedtest 的事件名是 **`SpeedtestStream`**（不是 `SpeedtestDotNet`）；MTR 是 `MTROutput`、Traceroute 是 `TracerouteOutput`。详见第 6 节。
- **目标参数报错**：必须用 `ip=` 而不是 `host=`。
- **`403 工具已被管理员禁用`**：网关开启了 Network Tools 开放控制（见 `NetMirror-Gateway-Docker教程.md` 第 10 节），当前节点把该工具设为 `false`。联系管理员开放，或改用未被禁用的节点。
- **经网关 Panel 模式 401/302**：没带登录 Cookie，先按第 7 节登录。
- **iPerf3 无数据**：确认对端 `iperf3 -s` 已起且端口可达（默认 5201），且目标 IP 填的是 server 端。
- **IPv6 测试被一并禁用**：禁用 `ping` 会同时禁 `ping6`，`mtr`/`traceroute` 同理（IPv6 共用 IPv4 开关）。
