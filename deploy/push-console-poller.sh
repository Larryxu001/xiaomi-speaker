#!/usr/bin/env bash
# 把控制台轮询脚本、它的配置文件和新版 init.sh 推送到音箱。
# 用法: XIAOAI_CONSOLE_URL=... XIAOAI_DEVICE_TOKEN=... deploy/push-console-poller.sh
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${XIAOAI_SSH_HOST:-xiaoai}
: "${XIAOAI_CONSOLE_URL:?set XIAOAI_CONSOLE_URL to the console base URL, e.g. https://xiaomi.speaker.xulangli.com.cn}"
: "${XIAOAI_DEVICE_TOKEN:?set XIAOAI_DEVICE_TOKEN to the device_token shown in the console (first run)}"

tmp_env=$(mktemp)
trap 'rm -f "$tmp_env"' EXIT
cat > "$tmp_env" <<EOF
XIAOAI_CONSOLE_URL=$XIAOAI_CONSOLE_URL
XIAOAI_DEVICE_TOKEN=$XIAOAI_DEVICE_TOKEN
EOF

ssh -o BatchMode=yes "$HOST" 'mkdir -p /data/open-xiaoai'
scp -O -q deploy/console-poller.sh "$HOST":/data/open-xiaoai/console-poller.sh
scp -O -q deploy/say.sh "$HOST":/data/open-xiaoai/say.sh
scp -O -q deploy/home.sh "$HOST":/data/open-xiaoai/home.sh
scp -O -q "$tmp_env" "$HOST":/data/open-xiaoai/console.env
scp -O -q deploy/init.sh "$HOST":/data/init.sh
ssh -o BatchMode=yes "$HOST" '
  chmod +x /data/open-xiaoai/console-poller.sh /data/open-xiaoai/say.sh /data/open-xiaoai/home.sh /data/init.sh
  chmod 600 /data/open-xiaoai/console.env
  # restart just the poller for this session without rebooting the speaker
  # Stop the running poller by its PID file (pkill -f would also match this
  # remote shell, whose own command line contains the script name).
  old=$(cat /tmp/xiaoai-console-poller/poller.pid 2>/dev/null)
  [ -n "$old" ] && kill "$old" 2>/dev/null
  rm -f /tmp/xiaoai-console-poller/poller.pid
  # BusyBox here has no nohup/setsid: ignore SIGHUP in the subshell instead,
  # which survives exec, so the poller keeps running after ssh disconnects.
  ( trap "" HUP
    . /data/open-xiaoai/console.env
    export XIAOAI_CONSOLE_URL XIAOAI_DEVICE_TOKEN
    exec /data/open-xiaoai/console-poller.sh ) </dev/null >>/data/open-xiaoai/console-poller.log 2>&1 &
  sleep 1
  tail -5 /data/open-xiaoai/console-poller.log 2>/dev/null || true
'
echo "pushed console poller; pointing at $XIAOAI_CONSOLE_URL"
