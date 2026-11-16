#!/usr/bin/env bash
# Enable the host-side app installer on the server (run once; safe to re-run).
#   sudo ~/xiaomi-speaker/deploy/setup-installer.sh
# It installs no apps: the catalog in apps/ decides what can be installed.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)

install -d -m 0755 /etc/xiaomi-speaker /opt/xiaomi-speaker-apps
if [ ! -s /etc/xiaomi-speaker/installer.secret ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/xiaomi-speaker/installer.secret
fi
chmod 0600 /etc/xiaomi-speaker/installer.secret

cat > /etc/systemd/system/xiaomi-speaker-installer.service <<EOF
[Unit]
Description=Xiaomi speaker console app installer (Unix socket only)
After=docker.service nginx.service
Wants=docker.service

[Service]
Environment=INSTALLER_CATALOG=$REPO/apps
Environment=INSTALLER_APPS_HOME=/opt/xiaomi-speaker-apps
Environment=INSTALLER_SECRET_FILE=/etc/xiaomi-speaker/installer.secret
ExecStart=/usr/bin/python3 -I $REPO/installer/installer.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now xiaomi-speaker-installer
systemctl restart xiaomi-speaker-installer
sleep 1
systemctl --no-pager --lines=3 status xiaomi-speaker-installer | head -6

# Apps are served under the console's address (/apps/<id>/); the console's
# nginx server block includes their snippets. Idempotent.
sudo mkdir -p /etc/nginx/xiaomi-speaker-apps
SITE=/etc/nginx/sites-available/xiaoai-console
if [ -f "$SITE" ] && ! sudo grep -q "xiaomi-speaker-apps" "$SITE"; then
  sudo cp "$SITE" "$SITE.bak.$(date +%s)"
  sudo sed -i '0,/client_max_body_size 4g;/s//client_max_body_size 4g;\n    include \/etc\/nginx\/xiaomi-speaker-apps\/*.conf;/' "$SITE"
  sudo nginx -t && sudo systemctl reload nginx
fi
