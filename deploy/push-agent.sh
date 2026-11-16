#!/usr/bin/env bash
# 把本地 xiaoai-agent/agent.yaml（以及可选的二进制/启动脚本）推送到音箱并重启 Agent。
# 用法: deploy/push-agent.sh [config|all] [start|restart|stop|status]
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${XIAOAI_SSH_HOST:-xiaoai}
WHAT=${1:-config}
ACTION=${2:-restart}
ssh -o BatchMode=yes "$HOST" 'mkdir -p /data/open-xiaoai'
scp -O -q xiaoai-agent/agent.yaml "$HOST":/data/open-xiaoai/agent.yaml
if [ "$WHAT" = "all" ]; then
  scp -O -q xiaoai-agent/target/armv7-unknown-linux-gnueabihf/release/xiaoai-agent "$HOST":/data/open-xiaoai/xiaoai-agent
  scp -O -q xiaoai-agent/start-agent.sh "$HOST":/data/open-xiaoai/start-agent.sh
  ssh -o BatchMode=yes "$HOST" 'chmod +x /data/open-xiaoai/xiaoai-agent /data/open-xiaoai/start-agent.sh'
fi
echo "pushed ($WHAT); running: start-agent.sh $ACTION"
ssh -o BatchMode=yes "$HOST" "RUST_LOG=${RUST_LOG:-info} /data/open-xiaoai/start-agent.sh $ACTION"
