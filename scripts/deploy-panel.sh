#!/usr/bin/env bash
#
# deploy-panel.sh — NetMirror 双面板互管一键部署脚本
#
# 用法:
#   ./deploy-panel.sh <本机IP> <对端IP> <ADMIN_KEY> [GHCR_TOKEN]
#   ./deploy-panel.sh <本机IP> none      <ADMIN_KEY> [GHCR_TOKEN]   # 单机部署(不注册对端)
#
# 功能(在一台机器上自动完成):
#   1. 放行防火墙 3000 端口
#   2. 拉取 ghcr.io/asxiaowen/netmirror-panel:fixed 镜像(私有镜像用第4参数登录)
#   3. 以 host 网络启动 panel 容器(自带完整后端, 可同时充当节点)
#   4. 在【本机面板】里把【自己】注册为节点
#   5. 在【本机面板】里把【对端】注册为节点(若给了对端IP)
#
# 互管拓扑:
#   机器A: ./deploy-panel.sh 246_IP 247_IP KEY
#   机器B: ./deploy-panel.sh 247_IP 246_IP KEY
#   => A 面板含 {A, B} 节点, B 面板含 {B, A} 节点, 实现双向互管
#
set -euo pipefail

# ---------- 参数解析 ----------
SELF_IP="${1:?用法: $0 <本机IP> <对端IP> <ADMIN_KEY> [GHCR_TOKEN]}"
PEER_IP="${2:-none}"
ADMIN_KEY="${3:?缺少 ADMIN_API_KEY}"
GHCR_TOKEN="${4:-${GHCR_TOKEN:-}}"

IMAGE="ghcr.io/asxiaowen/netmirror-panel:fixed"
PORT=3000
DATA_DIR="/opt/netmirror/data"
LOCATION_NAME="${LOCATION_NAME:-self-${SELF_IP}}"

# ---------- 前置检查 ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ 未检测到 docker, 请先安装: https://get.docker.com/" >&2
  exit 1
fi

# ---------- 1. 拉取镜像(私有仓库需登录) ----------
pull_image() {
  if docker pull "$IMAGE" 2>/dev/null; then
    echo "✓ 镜像拉取成功(匿名)"
    return 0
  fi
  if [ -n "$GHCR_TOKEN" ]; then
    echo "→ 匿名拉取失败, 尝试用提供的 GHCR_TOKEN 登录..."
    printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u asxiaowen --password-stdin
    docker pull "$IMAGE"
    echo "✓ 镜像拉取成功(已登录)"
    return 0
  fi
  echo "✗ 无法拉取镜像 $IMAGE" >&2
  echo "  该镜像为私有, 请在每台机器先执行:  docker login ghcr.io" >&2
  echo "  或把 GHCR_TOKEN 作为第4个参数传入本脚本。" >&2
  exit 1
}
pull_image

# ---------- 2. 防火墙放行 ----------
echo "→ 配置防火墙放行 ${PORT}/tcp"
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null || true
  firewall-cmd --reload 2>/dev/null || true
fi
if command -v ufw >/dev/null 2>&1; then
  ufw allow ${PORT}/tcp 2>/dev/null || true
fi

# ---------- 3. 启动 panel 容器 ----------
echo "→ 启动 panel 容器 (LOCATION=${LOCATION_NAME})"
docker rm -f netmirror-panel 2>/dev/null || true
docker run -d \
  --name netmirror-panel \
  --restart always \
  --network host \
  -e HTTP_PORT="${PORT}" \
  -e ADMIN_API_KEY="${ADMIN_KEY}" \
  -e LOCATION="${LOCATION_NAME}" \
  -v "${DATA_DIR}":/data \
  "${IMAGE}"

# ---------- 4. 等待面板就绪 ----------
echo "→ 等待面板启动 (最多 60s)..."
READY=0
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 \
     || curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" -ne 1 ]; then
  echo "✗ 面板在 60s 内未就绪, 请检查: docker logs netmirror-panel" >&2
  exit 1
fi
echo "✓ 面板已就绪: http://${SELF_IP}:${PORT}"

# ---------- 5. 节点注册函数 ----------
# 在 $BASE 面板上创建 deploy token 并把 $NODE_URL 注册为节点
register_node() {
  local base="$1" node_url="$2" node_label="$3"
  echo "→ 在 ${base} 注册节点 ${node_label} (${node_url})"

  local resp tok
  resp=$(curl -fsS -X POST "${base}/api/admin/tokens" \
    -H "X-API-Key: ${ADMIN_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"name":"auto-'"${node_label}"'","location":"HKG1-${node_label}","expires_in":0}' 2>/dev/null) || {
      echo "  ✗ 在 ${base} 创建 token 失败 (请确认 ADMIN_API_KEY 正确且面板在线)" >&2
      return 1
    }

  # 兼容多种返回格式: {"token":"..."} 或 {"data":{"token":"..."}}
  tok=$(printf '%s' "$resp" | grep -o '"token"[ ]*:[ ]*"[^"]*"' | head -1 | sed 's/.*:"\([^"]*\)".*/\1/')
  if [ -z "$tok" ]; then
    echo "  ✗ 解析 token 失败, 面板原始返回: ${resp}" >&2
    return 1
  fi

  curl -fsS -X POST "${base}/api/register" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${tok}\",\"url\":\"${node_url}\"}" 2>/dev/null \
    && echo "  ✓ 节点 ${node_label} 已注册到 ${base}" \
    || echo "  ✗ 节点 ${node_label} 注册失败 (请确认 ${node_url} 可达)"
}

# 注册自己 -> 本机面板
register_node "http://127.0.0.1:${PORT}" "http://${SELF_IP}:${PORT}" "${SELF_IP}" \
  || echo "  (本机节点注册失败, 可稍后在面板手动添加)"

# 注册对端 -> 本机面板
if [ "$PEER_IP" != "none" ] && [ -n "$PEER_IP" ]; then
  register_node "http://127.0.0.1:${PORT}" "http://${PEER_IP}:${PORT}" "${PEER_IP}" \
    || echo "  (对端节点注册失败, 请确认对端面板在线且 ADMIN_API_KEY 一致)"
else
  echo "→ 未提供对端IP, 跳过对端注册(单机模式)"
fi

echo ""
echo "========== 部署完成 =========="
echo "本机面板:  http://${SELF_IP}:${PORT}"
echo "节点位置:  ${LOCATION_NAME}"
echo "管理API密钥: ${ADMIN_KEY}"
echo ""
echo "提示: 在另一台机器以 互换的本机/对端IP 再跑一次本脚本, 即可实现双面板互管。"
