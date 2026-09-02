# netmirror-gateway

NetMirror 网络测速平台的**部署与访问控制**工具集：原生 Panel/Agent 镜像部署脚本、一个纯标准库 Python 反向代理网关（登录 / 访问控制 / Network Tools 开放控制），以及全套搭建教程。

> 镜像（`ghcr.io/asxiaowen`）：`netmirror-panel:fixed`、`netmirror-agent:fixed`、`netmirror-gateway:latest`
> 均已公开，匿名可直接 `docker pull`，无需登录。

---

## 这是什么

- **主控（Panel）**：带 Web UI 的测速面板，能调度所有节点，也能自身当测速节点。
- **被控（Agent）**：纯测速后端，无 UI，被主控调度。
- **网关（Gateway）**：可选。在面板/节点前做登录鉴权、真实访客 IP 还原、Network Tools 逐项开放/禁止控制。

---

## 仓库结构

```
netmirror-gateway/
├── login/                 # 网关源码（纯标准库，零第三方依赖）
│   ├── gateway.py         #   反代网关主程序
│   └── Dockerfile         #   基于 python:3-alpine
├── scripts/               # 部署脚本（已脱敏，IP/密码以 <...> 占位）
│   ├── deploy-panel.sh        # 一键部署主控(Panel)
│   ├── bootstrap-agent.sh     # 裸机一键加入被控(Agent)
│   ├── add-nodes.sh           # 批量注册 Agent 进主控
│   ├── build_push_gateway.py  # 构建/推送网关镜像到 ghcr
│   ├── upgrade_gateway_mtr_fix.py
│   ├── update_248_gateway.py
│   ├── nm_deploy_panel.py / nm_deploy_agent.py / nm_register.py
│   └── convert_247_to_agent.py
├── client/
│   └── nm_api_client.py   # NetMirror API 客户端（SSE 会话 + 下发测速）
└── docs/                  # 教程
    ├── NetMirror-主控被控搭建教程.md      # ★ 从零搭建 Panel + Agent
    ├── NetMirror-Gateway-Docker教程.md    # 网关镜像部署 / Network Tools 控制
    ├── NetMirror-测速API教程.md           # Speedtest / iPerf3 程序化调用
    ├── NetMirror-新机器部署教程.md
    ├── NetMirror-节点接入操作文档.md
    ├── NetMirror-登录与访问控制操作文档.md
    └── NetMirror-CORS-修复教程.md
```

---

## 快速开始

### 部署一套 主控 + 被控

```bash
# 1) 主控（Panel）
./scripts/deploy-panel.sh <主控IP> none <ADMIN_API_KEY>

# 2) 被控（Agent，连 Docker 都没装也能一键加入）
./scripts/bootstrap-agent.sh <主控IP> <被控IP> <ADMIN_API_KEY>
```

打开 `http://<主控IP>:3000` → 设置 → 节点列表，应看到主控(self) 与被控都 `online`。

详细步骤、API 契约、排障见 **[docs/NetMirror-主控被控搭建教程.md](docs/NetMirror-主控被控搭建教程.md)**。

### 加网关做登录/访问控制

见 **[docs/NetMirror-Gateway-Docker教程.md](docs/NetMirror-Gateway-Docker教程.md)**。

---

## 安全须知

- 本仓库所有部署脚本/文档中的 `IP`、`密码`、`ADMIN_API_KEY`、`GATEWAY_TOKEN` 均已替换为 `<...>` 占位符，**请勿把真实密钥提交进仓库**。
- 真正部署时，请自行设置强随机 `ADMIN_API_KEY`，并在网关数据目录放置 `users.txt` / `admin.key`。

## 许可

内部/自部署用途。NetMirror 上游为开源项目，请遵守其许可证。
