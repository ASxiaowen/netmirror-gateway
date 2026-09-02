# NetMirror LibreSpeed 上传为 0 —— CORS 修复教程

> 适用版本：`soyorins/netmirror-panel:latest` / `soyorins/netmirror-agent:latest`（2025-12-23 构建，官方目前唯一版本）
> 修复日期：2026-08-28　环境：CentOS 7.9 + Docker 26.1.4，面板 <PANEL_HOST>，节点 <AGENT1_HOST>
> 结果：下载 + 上传均正常出数 ✅

---

## 一、问题现象

在面板（246）选中子节点（247）跑 **LibreSpeed** 模式测速：

| 项目 | 表现 |
|---|---|
| 下载（Download） | ✅ 曲线正常、数值正常 |
| 上传（Upload） | ❌ 曲线能画，最终数值恒为 **0 Mbps** |

面板自身节点同样存在（换用「基于文件的测试 / File-based Test」模式则完全正常）。

---

## 二、排查过程（证据链）

### 第 1 步：看 agent 日志，判断请求到底有没有到

```bash
docker logs -f --tail 0 netmirror-agent
```

输出全是这种：

```
OPTIONS /session/xxxx/speedtest/upload   204
GET     /session/xxxx/speedtest/download 200
OPTIONS /session/xxxx/speedtest/upload   204
GET     /session/xxxx/speedtest/download 200
...
```

**关键结论**：只有 `OPTIONS`（预检）和 `GET`（下载），**一条真正的 `POST /upload` 都没有**。
→ 上传请求被浏览器在发出前就拦掉了，属于典型的 **CORS 跨域问题**，不是后端处理失败。

> 为什么下载不受影响？下载是 `GET`，属于「简单请求」，浏览器不预检、直接放行；
> 上传是 `POST` + 自定义请求头，必须先过 `OPTIONS` 预检。

### 第 2 步：找前端到底发了什么头

```bash
grep -n "setRequestHeader\|Content-Encoding" ui/speedtest/speedtest_worker.js
```

```
485:  xhr.setRequestHeader("Content-Encoding", "identity");
521:  xhr.setRequestHeader("Content-Encoding", "identity");
```

→ 上传时浏览器会带 `Content-Encoding: identity` 这个头。

### 第 3 步：看后端允许了什么头

`backend/als/route.go` 全局 CORS 中间件（修改前）：

```go
c.Writer.Header().Set("Access-Control-Allow-Origin", "*")
c.Writer.Header().Set("Access-Control-Allow-Credentials", "true")
c.Writer.Header().Set("Access-Control-Allow-Headers",
    "Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization, accept, origin, Cache-Control, X-Requested-With, session, X-Api-Key")
c.Writer.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS, GET, PUT, DELETE")
```

**两个致命 bug 全在这里。**

---

## 三、根因

### Bug 1：`Allow-Origin: *` 与 `Allow-Credentials: true` 非法共存

W3C 规范明确禁止：带凭据（`credentials`）的跨域请求，`Access-Control-Allow-Origin` **不能是 `*`**，必须是具体的 Origin。

后果：预检虽返回 204，但浏览器判定响应非法，实际 POST 不发。

### Bug 2：`Allow-Headers` 硬编码列表里缺 `Content-Encoding`

浏览器预检时会问："我可以发 `content-encoding` 头吗？"
服务器回的允许列表里没有它 → **预检判定失败 → POST 永远发不出去**。

这正好解释日志里"OPTIONS 全部 204，但 POST 一条没有"的矛盾现象。

### Bug 3（连带）：SSE 会话接口单独覆写了 `*`

`backend/als/controller/session/session.go` 里有单独一行也会把 Origin 写回 `*`，
即使修好了全局中间件，跨域 SSE 流仍会踩同样的坑。

---

## 四、修复方案

核心思路：**不要硬编码，改成"浏览器问什么就允许什么"（回显）**——
Origin 回显请求方，Allow-Headers 回显预检的 `Access-Control-Request-Headers`。

### 改动 1：`backend/als/route.go`

```go
e.Use(func(c *gin.Context) {
    // 回显请求方 Origin，让 Allow-Credentials: true 合法
    origin := c.Request.Header.Get("Origin")
    if origin == "" {
        origin = "*"
    }
    c.Writer.Header().Set("Access-Control-Allow-Origin", origin)
    c.Writer.Header().Set("Access-Control-Allow-Credentials", "true")

    // 回显预检请求的头列表（LibreSpeed 上传用到 Content-Encoding: identity）
    reqHeaders := c.Request.Header.Get("Access-Control-Request-Headers")
    if reqHeaders == "" {
        reqHeaders = "Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization, accept, origin, Cache-Control, X-Requested-With, session, X-Api-Key, Content-Encoding"
    }
    c.Writer.Header().Set("Access-Control-Allow-Headers", reqHeaders)
    c.Writer.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS, GET, PUT, DELETE")
    c.Writer.Header().Set("Access-Control-Max-Age", "86400")   // 预检结果缓存 24h，减少 OPTIONS 次数

    if c.Request.Method == "OPTIONS" {
        c.AbortWithStatus(204)
        return
    }

    c.Next()
})
```

### 改动 2：`backend/als/controller/session/session.go`

删掉这一行（交给全局中间件统一处理）：

```go
- c.Writer.Header().Set("Access-Control-Allow-Origin", "*")
```

完整 patch 见同目录 **`netmirror-cors-fix.patch`**，可直接 `git apply`。

---

## 五、构建与部署

> ⚠️ **面板（panel）和节点（agent）两端都要修、都要重新 build。**
> 只修 agent 的话，面板自身节点测速仍然会失败。

### 5.1 准备源码

```bash
git clone --no-single-branch --depth 1 https://github.com/catcat-blog/NetMirror.git netmirror-src
cd netmirror-src
git apply ../netmirror-cors-fix.patch     # 或直接按第四节手改两个文件
```

### 5.2 打包并上传到目标机

**⚠️ 最容易踩的坑**：构建脚本读的是打包好的 `nm-src.tar.gz`，
**每次改完源码必须重新打包**，否则 build 出来的还是旧代码。

```bash
rm -f nm-src.tar.gz
tar --exclude=netmirror-src/.git -czf nm-src.tar.gz netmirror-src
```

上传到服务器（我这里用 `nm_build_agent.py` / `nm_build_panel.py` 走 SFTP，手工的话 `scp` 即可）：

```bash
scp nm-src.tar.gz root@<AGENT1_HOST>:/root/
scp nm-src.tar.gz root@<PANEL_HOST>:/root/
```

### 5.3 在目标机上 build

**247（agent）**：

```bash
mkdir -p /root/nm-build && cd /root/nm-build
rm -rf netmirror-src && tar xzf /root/nm-src.tar.gz
cd netmirror-src
docker build -f Dockerfile.agent -t netmirror-agent:fixed .
```

**246（panel）**：

```bash
mkdir -p /root/nm-build && cd /root/nm-build
rm -rf netmirror-src && tar xzf /root/nm-src.tar.gz
cd netmirror-src
docker build -f Dockerfile -t netmirror-panel:fixed .
```

> 注：Dockerfile 里会 `apk` 装依赖 + Go 编译，耗时几分钟，耐心等。

### 5.4 替换容器

**247（agent）**：

```bash
docker rm -f netmirror-agent
docker run -d \
  --name netmirror-agent \
  --restart always \
  --network host \
  -e AGENT_MODE=true \
  -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-Node2' \
  -e PUBLIC_IPV4=<AGENT1_HOST> \
  netmirror-agent:fixed
```

**246（panel）**：

```bash
docker rm -f netmirror-panel
docker run -d \
  --name netmirror-panel \
  --restart always \
  --network host \
  -e ADMIN_API_KEY=<ADMIN_API_KEY> \
  -e HTTP_PORT=3000 \
  -e LOCATION='HKG1-SPT01-CNO' \
  -v /opt/netmirror/data:/data \
  netmirror-panel:fixed
```

> 换镜像不影响节点列表（数据存在 `/data` 卷里），重启后节点仍在。

---

## 六、验证

### 6.1 预检是否放行 `content-encoding`（最关键）

在 247 上执行，模拟浏览器从 246 跨域上传：

```bash
curl -s -i -X OPTIONS \
  -H 'Origin: http://<PANEL_HOST>:3000' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-encoding' \
  http://127.0.0.1:3000/session/test/speedtest/upload | grep -i -E 'HTTP|Access-Control'
```

**正确结果**：

```
HTTP/1.1 204 No Content
Access-Control-Allow-Credentials: true
Access-Control-Allow-Origin: http://<PANEL_HOST>:3000   ← 回显，不是 *
Access-Control-Allow-Headers: content-encoding           ← 回显预检问的头
Access-Control-Allow-Methods: POST, OPTIONS, GET, PUT, DELETE
Access-Control-Max-Age: 86400
```

❌ 如果 `Allow-Origin` 还是 `*`，说明跑的是旧镜像，回去检查 5.2 有没有重新打包。

### 6.2 真实 POST 上传（绕开浏览器，直接验证后端）

```python
import requests

base = 'http://<AGENT1_HOST>:3000'

# 1. 从 SSE 拿 session id
r = requests.get(f'{base}/session', stream=True,
                 headers={'Accept': 'text/event-stream'}, timeout=15)
sid = next(l.decode()[5:].strip() for l in r.iter_lines() if l.decode().startswith('data:'))
r.close()

# 2. 真实上传 1MB
r = requests.post(
    f'{base}/session/{sid}/speedtest/upload',
    data=b'x' * (1024 * 1024),
    headers={'Origin': 'http://<PANEL_HOST>:3000', 'Content-Encoding': 'identity'},
    timeout=30,
)
print(r.status_code, r.text[:120])   # 期望 200
```

### 6.3 浏览器实测

1. 打开 `http://<PANEL_HOST>:3000`
2. **`Ctrl + F5` 强制刷新**（必须！否则浏览器还在用缓存的旧 `speedtest_worker.js`）
3. 选 `HKG1-Node2`，LibreSpeed 模式跑一次 → 下载、上传都应有数值

### 6.4 看日志确认 POST 真的到了

```bash
docker logs -f --tail 0 netmirror-agent 2>&1 | grep -E 'speedtest|upload|POST'
```

修复前只有 `OPTIONS`，修复后应能看到 `POST .../speedtest/upload 200`。

---

## 七、排障速查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 上传 0，日志只有 OPTIONS 无 POST | Allow-Headers 没覆盖 `content-encoding` | 用 6.1 验证，重新 build |
| `Allow-Origin` 仍是 `*` | 跑的是旧镜像 / 改完源码没重新 tar | 重打包 → rebuild → 换容器 |
| 修好了但浏览器还是 0 | 浏览器缓存旧 JS | `Ctrl + F5` 强制刷新 |
| 只有节点上传失败，面板自身正常 | 只修了 panel 没修 agent | agent 也要 build |
| 只有面板自身失败，节点正常 | 只修了 agent 没修 panel | panel 也要 build |
| 面板本机节点测速数字 0（400 错误） | LibreSpeed 前端数值解析 bug（另一问题） | 用「基于文件的测试」模式 |
| agent 网页打开空白 | 正常，agent 无 UI（`ui:false`） | 统一在面板里管理 |

---

## 八、本次部署最终状态

| 机器 | 容器 | 镜像 | 端口 | 节点名 |
|---|---|---|---|---|
| <PANEL_HOST> | `netmirror-panel` | `netmirror-panel:fixed` | 3000 | HKG1-SPT01-CNO（面板本机） |
| <AGENT1_HOST> | `netmirror-agent` | `netmirror-agent:fixed` | 3000 | HKG1-Node2 |

节点通过 deploy token 注册（`POST /api/register {token, url}`），数据持久化在 246 的 `/opt/netmirror/data`。

---

## 九、附录：相关文件

| 文件 | 用途 |
|---|---|
| `netmirror-src/` | NetMirror 源码（已打补丁，depth 1） |
| `netmirror-cors-fix.patch` | 本次修复的 patch，可 `git apply` 到干净源码 |
| `nm_build_agent.py` | 上传源码到 247 并 build `netmirror-agent:fixed` |
| `nm_build_panel.py` | 上传源码到 246 并 build `netmirror-panel:fixed` |
| `nm_ssh.py` | SSH 执行模块（Windows 下先建 socket 再交 paramiko.Transport） |
| `nm-src.tar.gz` | 源码打包（**改源码后必须重新生成**） |
