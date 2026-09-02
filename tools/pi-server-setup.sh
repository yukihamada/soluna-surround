#!/usr/bin/env bash
# SOLUNA Sound — make a Raspberry Pi the SYNC SERVER (clock authority) of a venue.
# Pairs with pi-setup.sh (zero-config box). Since v7 every box can become the server by election;
# this script FORCES this box to be the server (writes /etc/soluna/force-server — the agent never
# yields and other boxes follow it). Use it for the FOH box; plain boxes need only pi-setup.sh.
#
#   curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-server-setup.sh \
#     | sudo -E bash                      # downloads the app from GitHub
#   SRC=/path/to/checkout bash tools/pi-server-setup.sh   # or install from a local copy (scp'd)
#
# Env:
#   PORT        HTTP/WS port (default 8900)
#   ADMIN       admin token (default: generated once → /opt/soluna/admin-token, chmod 600)
#   LOCAL_NODE  1 (default) = repoint an installed soluna-node.service to this server
#   AP          1 = also become a Wi-Fi access point (SSID $AP_SSID, pw $AP_PASS) via NetworkManager.
#               Phones join that Wi-Fi and open http://<pi>.local:8900/  — NOTE: turns wlan0 into
#               an AP, so run this over Ethernet/USB-Ethernet or you lose your Wi-Fi SSH session.
#   AP_SSID / AP_PASS   default SOLUNA / (generated, printed once)
#
# Scaling (measured, see README "Pi as the server"): CUE mode is control-plane only (one JSON per
# cue, media pre-distributed) so a Pi 4 holds thousands of WebSocket clients; the real ceiling is
# the Wi-Fi radio (a single Pi AP ≈ 30–50 phones). For a crowd, put the Pi on the venue LAN/Wi-Fi
# or run a 2nd Pi as hot standby (GET/POST /api/state) — the clock authority is one process.
set -euo pipefail
PORT="${PORT:-8900}"; APP=/opt/soluna; DATA=$APP/data; ENVF=/etc/soluna/server.env
LOCAL_NODE="${LOCAL_NODE:-1}"
USER_NAME="${SUDO_USER:-$(id -un)}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO=sudo

echo "▶ SOLUNA server install → $APP  (port=$PORT)"
$SUDO apt-get install -y -qq python3-aiohttp python3-pip python3-numpy python3-websockets \
  ffmpeg avahi-daemon rsync >/dev/null
python3 -c "import qrcode" 2>/dev/null || $SUDO pip3 install -q --break-system-packages qrcode

$SUDO mkdir -p "$APP" "$DATA/assets" /etc/soluna
if [ -n "${SRC:-}" ]; then
  $SUDO rsync -a --exclude .git --exclude '*.log' --exclude __pycache__ --exclude tests "$SRC"/ "$APP"/
else
  T=$(mktemp -d); curl -fsSL https://github.com/yukihamada/soluna-surround/archive/refs/heads/master.tar.gz \
    | tar -xz -C "$T" --strip-components=1
  $SUDO rsync -a --exclude tests "$T"/ "$APP"/; rm -rf "$T"
fi
# seed demo assets into the persistent data dir once (uploads land there too)
$SUDO rsync -a --ignore-existing "$APP/assets/" "$DATA/assets/"
$SUDO chown -R "$USER_NAME" "$APP"

# admin token: generate once, keep forever
if [ -z "${ADMIN:-}" ]; then
  if [ -s "$APP/admin-token" ]; then ADMIN=$(cat "$APP/admin-token")
  else ADMIN=$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32); fi
fi
umask 077; echo -n "$ADMIN" > "$APP/admin-token"; umask 022
$SUDO tee "$ENVF" >/dev/null <<ENV
PORT=$PORT
SOLUNA_ADMIN=$ADMIN
SOLUNA_DATA_DIR=$DATA
LAN_IP=$(hostname -I | awk '{print $1}')
PYTHONUNBUFFERED=1
ENV
$SUDO chmod 600 "$ENVF"

# kernel knobs for many concurrent WebSockets
$SUDO tee /etc/sysctl.d/90-soluna.conf >/dev/null <<'SYS'
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
fs.file-max = 200000
SYS
$SUDO sysctl -q --system >/dev/null 2>&1 || true

$SUDO tee /etc/systemd/system/soluna-server.service >/dev/null <<UNIT
[Unit]
Description=SOLUNA Sound sync server (clock authority)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=/usr/bin/python3 $APP/server.py
Restart=always
RestartSec=2
LimitNOFILE=65536
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT
$SUDO touch /etc/soluna/force-server          # soluna-agent: always server, never yield
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now soluna-server
$SUDO systemctl restart soluna-server
[ -f /etc/systemd/system/soluna-agent.service ] && $SUDO systemctl restart soluna-agent || true

# node on the same box → sync to localhost (zero network jitter)
if [ "$LOCAL_NODE" = "1" ] && [ -f /etc/systemd/system/soluna-node.service ]; then
  $SUDO sed -i -E "s#--server [^ ]+#--server ws://127.0.0.1:$PORT#" /etc/systemd/system/soluna-node.service
  $SUDO systemctl daemon-reload; $SUDO systemctl restart soluna-node
  echo "   local node repointed → ws://127.0.0.1:$PORT"
fi

# optional Wi-Fi access point (phones join directly, no venue network needed)
if [ "${AP:-0}" = "1" ]; then
  AP_SSID="${AP_SSID:-SOLUNA}"; AP_PASS="${AP_PASS:-$(head -c 12 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 12)}"
  $SUDO nmcli con delete soluna-ap >/dev/null 2>&1 || true
  $SUDO nmcli dev wifi hotspot ifname wlan0 con-name soluna-ap ssid "$AP_SSID" password "$AP_PASS" >/dev/null
  $SUDO nmcli con modify soluna-ap connection.autoconnect yes 802-11-wireless.band bg
  echo "   Wi-Fi AP up: SSID=$AP_SSID  password=$AP_PASS  (phones → http://$(hostname).local:$PORT/)"
fi

sleep 2
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
  echo "✅ server up: http://$(hostname -I | awk '{print $1}'):$PORT/   admin → /admin (token: $APP/admin-token)"
  echo "   other Pi nodes: SERVER=ws://$(hostname).local:$PORT bash pi-setup.sh"
  echo "   hot standby: on a 2nd box run this script, then POST /api/state from this one's GET /api/state"
else
  echo "🔴 server not answering on :$PORT — journalctl -u soluna-server -n 50"; exit 1
fi
