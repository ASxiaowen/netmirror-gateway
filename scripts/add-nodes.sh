#!/usr/bin/env bash
#
# add-nodes.sh — 把多台 agent 节点批量注册到「中心面板」
#
# 适用拓扑: 一台 panel(中心) + 多台 agent(节点)，适合一次加很多台。
# 前置: 每台新机器已启动 netmirror-agent (见下方 one-liner)，且中心面板 :3000 能访问它们。
#
# 用法:
#   ./add-nodes.sh <中心面板IP> <ADMIN_KEY> <节点IP1> [节点IP2 节点IP3 ...]
#
set -euo pipefail

CENTRAL="${1:?用法: $0 <中心面板IP> <ADMIN_KEY> <IP1> [IP2 ...]}"
KEY="${2:?缺少 ADMIN_API_KEY}"
shift 2
[ "$#" -ge 1 ] || { echo "✗ 至少给一个节点IP" >&2; exit 1; }

PORT=3000
echo "→ 向中心面板 http://${CENTRAL}:${PORT} 批量注册 $# 个节点"

for ip in "$@"; do
  echo "→ 处理节点 ${ip}"
  # 1) 中心面板建一个 deploy token
  resp=$(curl -fsS -X POST "http://${CENTRAL}:${PORT}/api/admin/tokens" \
    -H "X-API-Key: ${KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"node-${ip}\",\"location\":\"HKG1-${ip}\",\"expires_in\":0}" 2>/dev/null) || {
      echo "  ✗ 在中心面板创建 token 失败 (请确认 ADMIN_API_KEY 与面板在线)" >&2
      continue
    }
  tok=$(printf '%s' "$resp" | grep -o '"token"[ ]*:[ ]*"[^"]*"' | head -1 | sed 's/.*:"\([^"]*\)".*/\1/')
  if [ -z "$tok" ]; then
    echo "  ✗ 解析 token 失败, 面板返回: ${resp}" >&2
    continue
  fi
  # 2) 把该节点注册进中心面板
  curl -fsS -X POST "http://${CENTRAL}:${PORT}/api/register" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${tok}\",\"url\":\"http://${ip}:${PORT}\"}" 2>/dev/null \
    && echo "  ✓ ${ip} 已注册到中心面板" \
    || echo "  ✗ ${ip} 注册失败 (请确认该机 agent 已起且 :3000 可达)"
done

echo "完成。打开 http://${CENTRAL}:${PORT} → 设置 → 节点列表 查看。"
