#!/bin/bash
# ============================================================
# 拉取 apache/superset 镜像：显式国内镜像源 + 三级回退 + 本地补 tag
#
# 用法：./scripts/pull-image.sh [TAG]      默认 TAG=7.0.0rc2
# 作用：Docker Hub 大层直拉易断流（重连不支持 Range 续传），
#       显式走镜像源更稳；拉完补 tag 供 compose 引用
# ============================================================
set -u

TAG="${1:-7.0.0rc2}"
TARGET="docker.io/apache/superset:${TAG}"
PULLED=""

for MIRROR in docker.1ms.run docker.m.daocloud.io docker.nju.edu.cn; do
  echo "=== try pull ${MIRROR} ==="
  if podman pull --retry 5 --retry-delay 5s "${MIRROR}/apache/superset:${TAG}"; then
    PULLED="${MIRROR}/apache/superset:${TAG}"
    break
  fi
done

if [ -z "$PULLED" ]; then
  echo "=== all mirrors failed ==="
  exit 1
fi

echo "=== tag ${PULLED} -> ${TARGET} ==="
podman tag "$PULLED" "$TARGET"
echo "=== done: ${TARGET} ==="
