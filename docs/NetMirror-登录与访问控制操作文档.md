# NetMirror 登录与访问控制 操作文档

> 适用：<PANEL_HOST>（面板 panel / HKG1-SPT01-CNO，带登录页）、<AGENT1_HOST>（agent / HKG1-Node2）、<AGENT2_HOST>（agent / diag2）
> 目标：246 作为唯一面板，提供「账号 + 密码」登录页 + 多账号会话；247、248 作为纯 agent 节点（无 UI），公开暴露（NetMirror 面板 UI 需要浏览器直连 agent 进行 `/session`、`/nodes/latency`、`/method/...` 等调用）。

---

## 一、架构（关键变更）

原先 NetMirror 面板/agent 直接用 `--network host` 监听公网 `:3000`，完全开放。
现改为：

```
浏览器/调用方  ──:3000──▶  [网关 gateway]  ──127.0.0.1:3001──▶  [netmirror 容器]
 (公网)                     (登录校验/白名单)                      (仅本机可访问)
```

- **netmirror 容器**改为 bridge 模式，仅绑定 `127.0.0.1:3001`（本机回环，公网不可直连）。
- **网关 gateway** 监听公网 `:3000`：
  - 面板（仅 246）：自定义登录页，校验账号密码后签发 HttpOnly 会话 Cookie；`/api`、`/session`、`/method` 等仍需面板自带的 `ADMIN_API_KEY`（双层保护）。
  - Agent（247 / 248）：无登录页、无账号校验；以 `AGENT_MODE=true` 运行，代理所有请求到本机 `127.0.0.1:3001` 的 agent 容器。浏览器/面板可直接调用。
- 网关进程：
  - 246（面板网关）与 247（agent 网关）：均为 `python:3-alpine` 容器，`--network host`，`--restart always`。
  - 248（agent 网关）：Ubuntu 原生 `python3` + systemd 服务 `nm-gateway`。

> 为何不用 nginx：CentOS 7（内核 3.10）+ Docker 上 nginx 容器有 `/run/nginx.pid` 写入 bug；改用纯 Python(stdlib) 网关，零额外依赖、SSE 流式代理稳定。

---

## 二、默认账号与改密

部署时生成了初始账号（**请尽快修改**）：

- 账号：`admin`
- 密码：`<ADMIN_KEY>`

密码以 **PBKDF2-SHA256** 哈希存储在面板（246）的 `/opt/nm-gateway/users.txt`；247/248 为 agent 网关，不读取账号文件。
明文密码不在服务器留存。

### 增加 / 修改账号（多账号）

在对应机器上用 `mkuser.py`（随网关一起放在 `/opt/nm-gateway/`）：

```bash
# 新增或覆盖账号 user1
python3 /opt/nm-gateway/mkuser.py user1 '新密码' /opt/nm-gateway/users.txt

# 246（网关是容器）需重启容器使新账号生效：
docker restart nm-gateway

# 248（网关是 systemd）
systemctl restart nm-gateway
```

`users.txt` 每行格式：`用户名:salt(hex):pbkdf2(hex)`，可放任意多个账号。

---

## 三、用户登录流程（浏览器）

1. 打开 `http://<PANEL_HOST>:3000`（仅 246 有登录页；247/248 为 agent，无 UI）。
2. 未登录自动跳转到登录页 → 输入账号密码。
3. 校验通过，网关下发会话 Cookie，跳转回控制台正常使用（含 Ping / MTR / SSE 实时结果）。
4. 面板右上角有「退出登录」悬浮按钮（由网关注入，链接到 `/logout`）；点击即清除会话 Cookie 并跳回登录页。

> 未登录访问任何路径（含 `/api`、`/session`）都会被拦到登录页；仅凭 `ADMIN_API_KEY` 而无登录 Cookie 也会被拒（面板侧双重保护）。
> 注：NetMirror 自带的 Admin 子页面里的“登出”按钮只清前端 API key，不会清网关会话 Cookie；真正退出请用右上角网关注入的「退出登录」按钮。

---

## 四、面板如何调度 agent（247 / 248）

现仅 **246 为面板**，247 / 248 为纯 agent（无 UI）。NetMirror 的前端会直接从浏览器向各 agent 的 `:3000` 发起请求：
- 节点在线检测：`GET <agent>/nodes/latency?timestamp=...`
- 建立 SSE 会话：`EventSource <agent>/session`
- 触发测试：`GET <agent>/method/<cmd>?ip=...`，header `session: <SessionId>`

因此 agent 网关必须对浏览器来源公开（`AGENT_MODE=true`）。面板到 agent 的调用同样走 `http://<agent>:3000`，由网关转发到本机 `127.0.0.1:3001` 的 agent 容器。

---

## 五、Agent（247 / 248）访问控制

- agent 网关运行模式为 **`AGENT_MODE=true`（公开 agent）**，不设 `ALLOW_IPS`。原因：NetMirror 面板 UI 需要浏览器直连 agent 发起 `/session`、`/nodes/latency`、`/method/...` 等请求，IP 白名单会导致 UI 显示节点 Offline。
- 配置方式：
  - 247（容器网关）：`docker run ... -e AGENT_MODE=true ...`（见 `convert_247_to_agent.py`、`reinstall_agents_clean.py`）。
  - 248（systemd 网关）：`/etc/systemd/system/nm-gateway.service` 中 `Environment=AGENT_MODE=true`。
- 如确实需要把 agent 限制给某个固定来源（例如仅允许办公室 IP），可把 `ALLOW_IPS=<IP>` 加上；`gateway.py` 会同时进入 agent 模式并只放行该 IP（但面板 UI 从其他 IP 访问会再次 Offline）。
- `/login`、`/logout` 在 agent 模式下返回 404（无登录页）。

---

## 六、重启 / 排障

| 主机 | 重启网关 | 查看状态 | 查看日志 |
|------|----------|----------|----------|
| 246/247 | `docker restart nm-gateway` | `docker ps --filter name=nm-gateway` | `docker logs nm-gateway` |
| 248 | `systemctl restart nm-gateway` | `systemctl status nm-gateway` | `journalctl -u nm-gateway -n 50` |

- 面板/agent 容器：`docker restart netmirror-panel` / `docker restart netmirror-agent`（均 `--restart always`）。
- 端口检查：`ss -ltnp | grep ':3000\|:3001'`。
  - `:3000` 应为网关进程；`:3001` 应只绑定 `127.0.0.1`。
- 若登录页打不开：先确认网关进程在跑、`:3000` 在监听；再看网关日志有无异常（如 `users.txt` 权限）。

### 自检命令

```bash
# 面板：未登录应看到登录页（含「请输入账号」）
curl -s http://<PANEL_HOST>:3000/ | grep -c '请输入账号'

# 面板：带登录 Cookie + ADMIN_API_KEY 才能拿到数据（见仓库 test_sse_cookie.py）
# Agent：外部应能获取在线状态（200）
curl -s http://<AGENT1_HOST>:3000/nodes/latency?timestamp=$(date +%s)000
curl -s http://<AGENT2_HOST>:3000/nodes/latency?timestamp=$(date +%s)000
```

---

## 七、文件清单（位于本仓库 login/）

- `gateway.py` —— 网关主程序（登录 + 反向代理 + SSE 流式转发 + CORS/OPTIONS 预检代理 + agent 公开模式 + 面板 HTML 注入「退出登录」按钮），已部署到三台机器的 `/opt/nm-gateway/`。面板模式会对所有 `text/html` 响应注入右上角悬浮「退出登录」按钮（链接 `/logout`）。
- `mkuser.py` —— 账号管理工具（生成 PBKDF2 哈希写入 users.txt）。
- `users.txt` —— 账号哈希（部署时生成，`/opt/nm-gateway/users.txt`）。
- `deploy_login.py` —— 一键部署脚本：`python3 deploy_login.py <246|247|248> <panel|agent> [镜像]`（246 用 panel，248 用 agent；247 现已转 agent，改由 `convert_247_to_agent.py` 维护）。
- `convert_247_to_agent.py` —— 将 247 由面板转换为纯 agent 的一键脚本（停面板+登录网关 → 起 agent+公开网关），节点 URL 不变故 246 面板无需重新注册。
- `reinstall_agents_clean.py` —— **彻底重装** 247/248 为纯 agent：先 `docker rm -f` 全部相关容器 + 清 `/opt/netmirror/data`，再按各自模式（247=容器网关、248=systemd 网关）拉起 `netmirror-agent:fixed` + 公开 agent 网关（`AGENT_MODE=true`）。适用于「移除已装组件、重新装成 agent」的场景。
- `fix_agent_public_mode.py` —— 仅重刷 agent 网关：上传最新 `gateway.py` 并把 247/248 网关切到 `AGENT_MODE=true` 公开模式；同时刷新 246 网关以支持 OPTIONS 预检。
- `test_sse_cookie.py` —— 带登录 Cookie 的 SSE 端到端验证脚本（本地运行，指向 246）。

> 注意：`.admin_pass` 仅本机留存初始密码，切勿上传/外传。

---

## 八、白屏/加载慢修复（性能优化）

**现象**：登录面板后卡几秒白屏才出来。
**根因**：面板后端（gin）对 JS/CSS 静态资源**既不压缩也不发缓存头**，浏览器每次进入都要下载约 **2.05 MB** 未压缩的 SPA 资源（vendor-markdown 1.1MB、vendor-charts 0.5MB 等），Vue 挂载前 `#app` 为空 → 表现为白屏。SSE 的 `Config` 事件本身很快（≈网络 RTT），不是瓶颈。

**修复（已部署到三台 `/opt/nm-gateway/gateway.py`）**：
1. **网关对静态文本资源（html/js/css）按需 gzip**：客户端带 `Accept-Encoding: gzip` 时压缩；SPA 总载荷 **2.05 MB → 0.61 MB（↓70%）**。SSE/JSON 不压缩（避免缓冲流式响应）。
2. **`Cache-Control` 缓存头**：内容哈希资源（`/js/*-*.js`、`/css/*-*.css`）返回 `public, max-age=31536000, immutable`，浏览器**只下载一次**；重复进入面板只需 index.html + SSE，几乎秒开。
3. **注入纯 CSS 加载动画**：面板模式在 `#app` 内注入 loading spinner（「正在加载 NetMirror…」），JS 未下载/解析前立即显示反馈，替代空白白屏；Vue 挂载后自动覆盖。

**部署/更新**：
```bash
python3 deploy_gateway.py            # 上传 gateway.py 并重启 246/247/248 网关
python3 deploy_gateway.py 246        # 仅单台
```
- 246/247：`docker restart nm-gateway`
- 248：`systemctl restart nm-gateway`（systemd 原生运行）

**验证**：
```bash
# 资源已 gzip + 永久缓存
curl -sI -H "Accept-Encoding: gzip" http://<PANEL_HOST>:3000/js/vendor-markdown-BDez1Xfm.js \
  | grep -iE 'content-encoding|cache-control'
# 应见： content-encoding: gzip
#        cache-control: public, max-age=31536000, immutable
```

---

## 九、分享测试链接（密码 + 超时，给别人测）

**需求**：生成一条带密码、带有效期的分享链接发给外部人员测试；过期后无法访问，可随时撤销。

**实现**：网关维护 `/opt/nm-gateway/shares.json`（`{id:{salt,phash,exp,created,note}}`），密码以 PBKDF2 哈希存储（明文不过网）。每次受保护请求都会校验 `exp`，过期即拒；文件 mtime 变化网关自动重载，**增删分享无需重启网关**。

**管理脚本 `gen_share_link.py`（指向面板 246）**：
```bash
# 创建：按提示输入密码，指定分钟数与可选备注
python3 gen_share_link.py create 60 "客户A测试"
#   -> 输出链接  http://<PANEL_HOST>:3000/share?id=<id>  与密码、过期时间

python3 gen_share_link.py list     # 列出所有分享（备注/过期时间/剩余/状态）
python3 gen_share_link.py show <id>
python3 gen_share_link.py revoke <id>   # 提前撤销
```
> 非交互/脚本环境可用 `echo 密码 | python3 gen_share_link.py create 60 备注` 传密码。

**对方使用流程**：
1. 打开 `http://<PANEL_HOST>:3000/share?id=<id>` → 输入你给的密码 → 进入面板。
2. 浏览器拿到 `nm_share` Cookie（有效期 = 剩余时长），之后正常测速/节点测试。
3. 链接过期或你 `revoke` 后，再访问会看到「测试链接已过期」页，且所有请求被拒（含正在进行的 SSE）。

**权限范围**：分享链接 = 完整 UI 访问（与登录账号同权限，可测速/看节点），但不含管理员 API Key，无法改设置。如需只读/限制范围可后续扩展。

**部署**：`python3 deploy_gateway.py` 已含分享逻辑；`shares.json` 由脚本远程写入 246（容器映射到 `/data/shares.json`）。

---

## 十、节点管理控制台（浏览器内操作，无需 SSH）

> 需求：用户希望直接在网页里「输入管理密钥进入 → 生成带密码+有效期的分享链接 / 管理节点」，不要再走 SSH 命令行。

访问 `http://<PANEL_HOST>:3000/console`：

1. **管理密钥登录**：首屏要求输入「管理密钥」。当前密钥 = 面板 `ADMIN_API_KEY`
   （`<ADMIN_API_KEY>`），写在网关主机的
   `/opt/nm-gateway/admin.key`（容器内 `/data/admin.key`）里，可用 `CONSOLE_KEY` 环境变量或
   `admin.key` 文件覆盖。
2. **生成测试分享链接**：填写「访问密码 / 有效时长(分钟) / 备注」→ 点「生成链接」，
   页面直接给出可复制的完整 URL（`http://<PANEL_HOST>:3000/share?id=<id>`）。
3. **已有分享链接**：表格列出 ID / 备注 / 过期时间 / 状态，支持「复制」与「撤销」（撤销即时生效）。
4. **节点管理**：表格列出全部节点（名称/位置/URL/是否本机），支持「添加节点」与「删除节点」。
   节点增删走面板 `/api/admin/nodes`，由网关自动带上面板 API Key（存于 `/opt/nm-gateway/panel.key`）。
5. **修改控制台密码**：在控制台内直接改「管理密钥」（写回 `admin.key`，下次登录用新密码）。
   注意：改控制台密码**不影响**节点管理——节点 API 用的是独立的 `panel.key`（面板 ADMIN_API_KEY）。

**鉴权边界**：
- `/console` 未带有效 `nm_admin` Cookie → 返回密钥登录页。
- `/console/api/*` 必须带 `nm_admin` Cookie，否则 403。
- 分享链接的创建/撤销在服务端写 `shares.json`（mtime 变更即热加载，无需重启网关）。
- 节点增删经网关代理到面板时自动注入 `X-Api-Key`，浏览器侧拿不到明文 Key。

**部署 / 维护**：
- 改完 `login/gateway.py` 后：`python3 deploy_gateway.py 246`（仅面板需控制台，247/248 为纯 agent 不提供此页）。
- 首次上线需在 246 写两个文件（部署脚本 `deploy_console.py` 已自动完成）：
  - `/opt/nm-gateway/admin.key` = 控制台入口密码
  - `/opt/nm-gateway/panel.key` = 面板 `ADMIN_API_KEY`（驱动节点 API）


