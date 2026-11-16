#!/usr/bin/env bash
# Ship the current main branch to the server and redeploy the console,
# without the server talking to GitHub (often unreachable from mainland
# China). Commits travel as a git bundle over SSH, so the server's checkout
# stays an exact copy of GitHub's history.
#   deploy/ship-to-server.sh            # uses ssh host alias xiaoai-console-srv
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${XIAOMI_SPEAKER_SERVER:-xiaoai-console-srv}
BUNDLE=$(mktemp -t xiaomi-speaker).bundle
trap 'rm -f "$BUNDLE"' EXIT

git bundle create -q "$BUNDLE" main
scp -q "$BUNDLE" "$HOST":/tmp/xiaomi-speaker.bundle
ssh "$HOST" 'set -e
  cd ~/xiaomi-speaker
  git pull -q --ff-only /tmp/xiaomi-speaker.bundle main
  rm -f /tmp/xiaomi-speaker.bundle
  echo "server now at $(git log --oneline -1)"
  cd console && sudo docker compose up -d --build >/dev/null 2>&1
  for i in $(seq 1 20); do curl -fs -o /dev/null http://127.0.0.1:8088/healthz && { echo "console healthy"; exit 0; }; sleep 1; done
  echo "console did not become healthy" >&2; sudo docker logs --tail 30 xiaoai-console >&2; exit 1'
