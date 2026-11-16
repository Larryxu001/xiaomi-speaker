#!/usr/bin/env bash
# Update the console on the server from GitHub and rebuild it.
# Run on the server:   ~/xiaomi-speaker/deploy/update-console.sh
# Or from your machine: ssh <server> '~/xiaomi-speaker/deploy/update-console.sh'
# console/data (passwords, keys, tasks) is git-ignored and never touched.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
cd console
sudo docker compose up -d --build
for i in $(seq 1 15); do
    if curl -fs -o /dev/null http://127.0.0.1:8088/healthz; then
        echo "console updated to $(git log --oneline -1)"
        exit 0
    fi
    sleep 1
done
echo "console did not become healthy; recent logs:" >&2
sudo docker logs --tail 40 xiaoai-console >&2
exit 1
