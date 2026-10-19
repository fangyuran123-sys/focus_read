#!/usr/bin/env bash
set -euo pipefail

APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if (( $# != 1 )); then
    echo "用法：$0 /实际/Kindle挂载点" >&2
    exit 2
fi
for required in docker mountpoint; do
    command -v "$required" >/dev/null || { echo "缺少依赖：$required" >&2; exit 1; }
done
if ! mountpoint -q -- "$1" || [[ ! -d "$1/documents" ]]; then
    echo 'Kindle 尚未挂载，或所选目录没有 documents；请先在宿主机挂载设备。' >&2
    exit 1
fi
if [[ ! -r "$1/documents" || ! -w "$1/documents" || ! -x "$1/documents" ]]; then
    echo '当前用户不能读写 Kindle documents，请检查宿主机挂载权限。' >&2
    exit 1
fi
if command -v systemctl >/dev/null && systemctl --user is-active --quiet kindle-desk.service; then
    echo '本机版仍在运行。请先执行 systemctl --user stop kindle-desk，再启动 Docker。' >&2
    exit 1
fi

export KINDLE_MOUNT=$(cd -- "$1" && pwd -P)
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
export KINDLE_IMAGE=${KINDLE_IMAGE:-kindle-desk:0.2.0}
export KINDLE_DATA=${KINDLE_DATA:-$APP_DIR/data}
cd -- "$APP_DIR"
docker info >/dev/null
docker compose version >/dev/null
if ! docker image inspect "$KINDLE_IMAGE" >/dev/null 2>&1; then
    echo "本机没有镜像 $KINDLE_IMAGE，请先 docker load -i 镜像包.tar.gz，或按 README 构建。" >&2
    exit 1
fi
mkdir -p -- "$KINDLE_DATA"
if [[ ! -w "$KINDLE_DATA" || ! -x "$KINDLE_DATA" ]]; then
    echo "数据目录不可写：$KINDLE_DATA" >&2
    exit 1
fi
# Recreate to bind the current USB mount after unplug/replug; never source .env as shell code.
docker compose --env-file /dev/null -f "$APP_DIR/compose.yaml" up -d --no-build --force-recreate --wait --wait-timeout 90
echo 'Kindle Desk 已运行：http://127.0.0.1:8765'
echo "拔线前：cd \"$APP_DIR\" && KINDLE_MOUNT=\"$KINDLE_MOUNT\" docker compose down，然后从宿主机安全弹出。"
