#!/usr/bin/env bash
#
# bootstrap-agent.sh — 裸机(可能没装 Docker)一键加入为中心面板的 agent 节点
#
# 用法:
#   ./bootstrap-agent.sh <中心面板IP> <本机IP> <ADMIN_KEY>
#
# 自动完成:
#   1. 检测并安装 Docker (官方 get.docker.com 一键脚本, 支持 CentOS7/Ubuntu/Debian)
#   2. 防火墙放行 3000
#   3. 起 netmirror-agent 容器 (host 网络, 可作被调度节点)
#   4. 向中心面板 POST /api/register 把本机注册为节点
#
# 前提:
#   - 本机能出网拉 ghcr.io 镜像
#   - 本机能访问中心面板 :3000, 且中心面板能回连本机 :3000
#
set -euo pipefail

CENTRAL="${1:?用法: $0 <中心面板IP> <本机IP> <ADMIN_KEY>}"
SELF_IP="${2:?缺少本机IP}"
KEY="${3:?缺少ADMIN_API_KEY}"
PORT=3000

# ---------- 1. 安装 Docker(若缺失) ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "→ 未检测到 docker, 使用官方脚本安装..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker 2>/dev/null || true
  # 等待 docker 守护进程就绪
  for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || { echo "✗ docker 安装后仍未就绪, 请检查"; exit 1; }
  echo "✓ docker 已安装"
else
  echo "→ docker 已存在, 跳过安装"
fi

# ---------- 2. 防火墙放行 ----------
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null || true
  firewall-cmd --reload 2>/dev/null || true
fi
if command -v ufw >/dev/null 2>&1; then
  ufw allow ${PORT}/tcp 2>/dev/null || true
fi

# ---------- 3. 启动 agent 容器 ----------
echo "→ 启动 netmirror-agent (LOCATION=HKG1-${SELF_IP})"
docker rm -f netmirror-agent 2>/dev/null || true
docker run -d --name netmirror-agent --restart always --network host \
  -e AGENT_MODE=true \
  -e HTTP_PORT="${PORT}" \
  -e LOCATION="HKG1-${SELF_IP}" \
  -e PUBLIC_IPV4="${SELF_IP}" \
  ghcr.io/asxiaowen/netmirror-agent:fixed

# ---------- 4. 等待 agent 就绪 ----------
echo "→ 等待 agent 启动 (最多 60s)..."
READY=0
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
[ "$READY" -eq 1 ] || { echo "✗ agent 未在 60s 内就绪, 看: docker logs netmirror-agent"; exit 1; }
echo "✓ agent 已就绪"

# ---------- 5. 到中心面板注册自己 ----------
echo "→ 向中心面板 ${CENTRAL}:${PORT} 注册本机"
resp=$(curl -fsS -X POST "http://${CENTRAL}:${PORT}/api/admin/tokens" \
  -H "X-API-Key: ${KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"node-${SELF_IP}\",\"location\":\"HKG1-${SELF_IP}\",\"expires_in\":0}" 2>/dev/null) || {
    echo "✗ 无法连接中心面板 ${CENTRAL}:${PORT} (请确认 IP/端口可达, KEY 正确)" >&2
    exit 1
  }

tok=$(printf '%s' "$resp" | grep -o '"token"[ ]*:[ ]*"[^"]*"' | head -1 | sed 's/.*:"\([^"]*\)".*/\1/')
if [ -z "$tok" ]; then
  echo "✗ 解析 token 失败, 中心面板返回: ${resp}" >&2
  exit 1
fi

curl -fsS -X POST "http://${CENTRAL}:${PORT}/api/register" \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"${tok}\",\"url\":\"http://${SELF_IP}:${PORT}\"}" 2>/dev/null \
  && echo "✓ 本机已注册到中心面板 ${CENTRAL}" \
  || echo "✗ 注册失败 (请确认中心面板能访问本机 ${SELF_IP}:${PORT})"

echo ""
echo "完成。打开 http://${CENTRAL}:${PORT} → 设置 → 节点列表 应能看到 HKG1-${SELF_IP}。"
