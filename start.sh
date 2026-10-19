#!/usr/bin/env bash
set -euo pipefail

APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SERVICE=kindle-desk.service
URL=http://127.0.0.1:8765

if (( $# )); then
    echo '直接运行 start.sh 即可。自定义端口或设备路径请使用 python3 server.py --help。' >&2
    exit 2
fi
for required in python3 systemctl systemd-run; do
    if ! command -v "$required" >/dev/null; then
        echo "缺少启动依赖：$required" >&2
        exit 1
    fi
done
PYTHON=$(command -v python3)

if systemctl --user is-active --quiet "$SERVICE"; then
    running_dir=$(systemctl --user show "$SERVICE" --property=WorkingDirectory --value)
    if [[ "$running_dir" != "$APP_DIR" ]]; then
        echo "同名服务来自其他目录：$running_dir。请先停止旧实例。" >&2
        exit 1
    fi
else
    systemd-run --user --unit="$SERVICE" --collect \
        --description='Kindle Desk local application' \
        --property="WorkingDirectory=$APP_DIR" \
        "$PYTHON" "$APP_DIR/server.py"
fi

"$PYTHON" - "$SERVICE" "$URL" <<'PY'
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

service, url = sys.argv[1:]
for attempt in range(30):
    active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', service]).returncode == 0
    try:
        if active:
            with urllib.request.urlopen(url + '/api/state', timeout=1) as response:
                state = json.load(response)
            if 'connected' in state and 'converter' in state:
                print('Kindle Desk 已运行：' + url)
                print('关闭此终端不会停止服务。停止命令：systemctl --user stop kindle-desk')
                break
    except (OSError, ValueError, urllib.error.URLError):
        pass
    time.sleep(0.5)
else:
    print('启动未成功，请查看日志：journalctl --user -u kindle-desk -n 30 --no-pager', file=sys.stderr)
    sys.exit(1)
PY
