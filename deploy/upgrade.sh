#!/usr/bin/env bash
# 服务端升级脚本：构建 qwenpaw:latest 并重建运行中的容器（保留数据卷）。
# 由 scripts/deploy_remote.py 以 detached 方式触发，全部输出写入 /data/qwenpaw/build.log。
# 也可手动运行：
#   cd /data/qwenpaw && QWENPAW_DEPLOY_BACKUP=1 bash deploy/upgrade.sh
set -euo pipefail
trap 'echo "[upgrade] BUILD_FAIL rc=$?"' ERR

REMOTE_DIR="/data/qwenpaw"
cd "$REMOTE_DIR"

TAG="${QWENPAW_IMAGE_TAG:-qwenpaw:latest}"
COMPOSE_FILE="${QWENPAW_COMPOSE_FILE:-deploy/docker-compose.prod.yml}"
DO_BACKUP="${QWENPAW_DEPLOY_BACKUP:-0}"

echo "===== [upgrade] start $(date) ====="

# 0) 防御性：把可能随上传带入 CRLF 的文本文件转回 LF（Windows 上传常见坑）
echo "[upgrade] normalizing CRLF->LF on text files (defensive)..."
find "$REMOTE_DIR" -type f \( -name '*.sh' -o -name 'Dockerfile*' -o -name '*.yml' \
  -o -name '*.yaml' -o -name '*.conf' -o -name '*.template' -o -name '*.py' \) \
  -exec sed -i 's/\r$//' {} + 2>/dev/null || true

# 1) 升级前可选备份数据卷（三个外部卷本身已持久化，此为额外保险）
if [ "$DO_BACKUP" = "1" ]; then
  BAK="/data/qwenpaw/volumes-bak/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BAK"
  for v in qwenpaw-data qwenpaw-secrets qwenpaw-backups; do
    echo "[upgrade] backing up volume $v ..."
    docker run --rm -v "$v":/src -v "$BAK":/dst busybox \
      tar czf "/dst/$v.tar.gz" -C /src . 2>/dev/null \
      && echo "[upgrade]   $v -> $BAK/$v.tar.gz" \
      || echo "[upgrade]   WARN: backup $v skipped (busybox image missing?)"
  done
fi

# 2) 构建镜像（BuildKit 必需：Dockerfile 使用了 COPY --chmod=755）
echo "[upgrade] building $TAG (BuildKit)..."
DOCKER_BUILDKIT=1 bash scripts/docker_build.sh "$TAG"
echo "[upgrade] BUILD_SUCCESS"

# 3) 重建容器（仅删容器，三个数据卷原封不动 -> 自动迁移/回退均可行）
echo "[upgrade] recreating container (volumes preserved)..."
docker rm -f qwenpaw 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" up -d
echo "[upgrade] UPGRADE_DONE $(date)"
