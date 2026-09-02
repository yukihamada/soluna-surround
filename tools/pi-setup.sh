#!/usr/bin/env bash
# SOLUNA Sound — one-shot install that turns a Raspberry Pi into a zero-config SOLUNA box.
# After this: power on → the box finds a server (or becomes one), plays as a speaker node,
# heals itself, and shows up in /admin NODES where you assign its zone. No per-box config.
#
#   curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh | bash
#   SRC=/path/to/checkout bash tools/pi-setup.sh          # install from a local copy (scp/rsync)
#
# Optional env (all have zero-config defaults):
#   SERVER   ws(s)://host[:port]  pin this node to one server (disables auto-discovery for the node)
#   CH       channel (festival)   ZONE/POS/GAIN_DB  initial zone letter / L|C|R / dB (else from /admin)
#   DEVICE   sounddevice name/index (auto = USB > GPIO I2S DAC > built-in)
#   DAC      GPIO I2S DAC overlay, e.g. hifiberry-dac (PCM5102A/MAX98357A), hifiberry-dacplus (PCM5122). Reboot after.
#   AP       0 to never raise the SOLUNA Wi-Fi AP when this box is the server (default 1)
#   ADMIN    admin token for the (standby) server on this box (default: generated once → /opt/soluna/admin-token)
set -euo pipefail
APP=/opt/soluna; DATA=$APP/data; ETC=/etc/soluna
CH="${CH:-festival}"; POS="${POS:-}"; ZONE="${ZONE:-}"; GAIN_DB="${GAIN_DB:-0}"; DEVICE="${DEVICE:-auto}"
PORT="${PORT:-8900}"
USER_NAME="${SUDO_USER:-$(id -un)}"; [ "$USER_NAME" = root ] && USER_NAME=pi
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO=sudo
echo "▶ SOLUNA box install → $APP  (user=$USER_NAME ch=$CH server=${SERVER:-auto})"

# ---- packages (node + server + agent) ---------------------------------------------------
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3-numpy python3-websockets python3-aiohttp python3-pip libportaudio2 \
  ffmpeg alsa-utils curl rsync avahi-daemon avahi-utils network-manager >/dev/null
python3 -c "import sounddevice" 2>/dev/null || $SUDO pip3 install -q --break-system-packages sounddevice
python3 -c "import qrcode" 2>/dev/null || $SUDO pip3 install -q --break-system-packages qrcode

# ---- app files ----------------------------------------------------------------------------
$SUDO mkdir -p "$APP" "$DATA/assets" "$ETC"
if [ -n "${SRC:-}" ]; then
  $SUDO rsync -a --exclude .git --exclude '*.log' --exclude __pycache__ --exclude tests "$SRC"/ "$APP"/
else
  T=$(mktemp -d); curl -fsSL https://github.com/yukihamada/soluna-surround/archive/refs/heads/master.tar.gz \
    | tar -xz -C "$T" --strip-components=1
  $SUDO rsync -a --exclude tests "$T"/ "$APP"/; rm -rf "$T"
fi
$SUDO rsync -a --ignore-existing "$APP/assets/" "$DATA/assets/" 2>/dev/null || true
$SUDO chown -R "$USER_NAME" "$APP"

# ---- env files ----------------------------------------------------------------------------
# node.env: SERVER=auto → agent.py fills in the discovered server; PINNED=1 when given explicitly.
if [ -n "${SERVER:-}" ] && [ "$SERVER" != auto ]; then PIN=1; SRV="$SERVER"; else PIN=0; SRV=auto; fi
$SUDO tee "$ETC/node.env" >/dev/null <<ENV
SERVER=$SRV
PINNED=$PIN
CH=$CH
POS=$POS
ZONE=$ZONE
GAIN_DB=$GAIN_DB
DEVICE=$DEVICE
PYTHONUNBUFFERED=1
ENV
# server.env: this box can become the clock authority at any time (agent decides) → needs a token.
if [ -z "${ADMIN:-}" ]; then
  if [ -s "$APP/admin-token" ]; then ADMIN=$(cat "$APP/admin-token")
  else ADMIN=$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32); fi
fi
( umask 077; echo -n "$ADMIN" | $SUDO tee "$APP/admin-token" >/dev/null ); $SUDO chown "$USER_NAME" "$APP/admin-token"; $SUDO chmod 600 "$APP/admin-token"
$SUDO tee "$ETC/server.env" >/dev/null <<ENV
PORT=$PORT
SOLUNA_ADMIN=$ADMIN
SOLUNA_DATA_DIR=$DATA
PYTHONUNBUFFERED=1
ENV
$SUDO chmod 600 "$ETC/server.env"
$SUDO tee "$ETC/agent.env" >/dev/null <<ENV
SOLUNA_AP=${AP:-1}
SOLUNA_AP_SSID=${AP_SSID:-SOLUNA}
SOLUNA_AP_BAND=${AP_BAND:-bg}
SOLUNA_DATA_DIR=$DATA
PORT=$PORT
PYTHONUNBUFFERED=1
ENV
# agent runs as root (systemctl / nmcli / /etc/soluna) → it must be able to read the token for /api/state.

# ---- GPIO I2S DAC overlay (optional, idempotent) ------------------------------------------
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
if [ -n "${DAC:-}" ] && [ -f "$CFG" ] && ! grep -q "^dtoverlay=${DAC}\b" "$CFG"; then
  echo "dtoverlay=${DAC}" | $SUDO tee -a "$CFG" >/dev/null
  echo "   I2S DAC overlay added: dtoverlay=${DAC} (reboot to activate)"; NEED_REBOOT=1
fi
# hardware watchdog: a hung kernel reboots itself (systemd pets it every 15 s)
if [ -f "$CFG" ] && ! grep -q "^dtparam=watchdog=on" "$CFG"; then
  echo "dtparam=watchdog=on" | $SUDO tee -a "$CFG" >/dev/null; NEED_REBOOT=1
fi
$SUDO mkdir -p /etc/systemd/system.conf.d
$SUDO tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null <<'WD'
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
WD
# journald: never fill the SD card with logs
$SUDO mkdir -p /etc/systemd/journald.conf.d
$SUDO tee /etc/systemd/journald.conf.d/soluna.conf >/dev/null <<'JD'
[Journal]
SystemMaxUse=50M
RuntimeMaxUse=30M
JD
$SUDO systemctl restart systemd-journald 2>/dev/null || true
# many WebSockets when this box is the server
$SUDO tee /etc/sysctl.d/90-soluna.conf >/dev/null <<'SYS'
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
fs.file-max = 200000
SYS
$SUDO sysctl -q --system >/dev/null 2>&1 || true

# ---- plug-and-play USB sound card → node restarts onto it ---------------------------------
$SUDO tee /etc/udev/rules.d/90-soluna-usb-audio.rules >/dev/null <<'RULE'
ACTION=="add", SUBSYSTEM=="sound", KERNEL=="card*", ENV{ID_BUS}=="usb", RUN+="/bin/systemctl --no-block restart soluna-node.service"
RULE
$SUDO udevadm control --reload 2>/dev/null || true

# ---- systemd units ------------------------------------------------------------------------
$SUDO tee /etc/systemd/system/soluna-node.service >/dev/null <<UNIT
[Unit]
Description=SOLUNA Sound speaker node
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$ETC/node.env
Environment=SOLUNA_NODE_JSON=$APP/node.json
ExecStart=/usr/bin/python3 $APP/play.py \$POS --server \${SERVER} --ch \${CH} --gain-db \${GAIN_DB} --device \${DEVICE}
Restart=always
RestartSec=3
Nice=-10

[Install]
WantedBy=multi-user.target
UNIT
# ZONE only when given (else the /admin assignment / node.json rules)
if [ -n "$ZONE" ]; then $SUDO sed -i "s|--device|--zone $ZONE --device|" /etc/systemd/system/soluna-node.service; fi

$SUDO tee /etc/systemd/system/soluna-server.service >/dev/null <<UNIT
[Unit]
Description=SOLUNA Sound sync server (clock authority) — started by soluna-agent when this box is elected
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$ETC/server.env
ExecStart=/usr/bin/python3 $APP/server.py
Restart=always
RestartSec=2
LimitNOFILE=65536
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT

$SUDO tee /etc/systemd/system/soluna-agent.service >/dev/null <<UNIT
[Unit]
Description=SOLUNA box supervisor (discover / elect / heal / report)
After=network-online.target avahi-daemon.service NetworkManager.service
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$APP
EnvironmentFile=$ETC/agent.env
ExecStart=/usr/bin/python3 $APP/agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable soluna-node soluna-agent >/dev/null 2>&1
$SUDO systemctl disable soluna-server >/dev/null 2>&1 || true      # the agent starts it when elected
[ -f "$ETC/force-server" ] && $SUDO systemctl enable soluna-server >/dev/null 2>&1 || true
$SUDO systemctl restart soluna-node soluna-agent
sleep 4
echo "   node:  $(systemctl is-active soluna-node)   agent: $(systemctl is-active soluna-agent)   server: $(systemctl is-active soluna-server)"
echo "✅ SOLUNA box ready. It will find a server on this network or become one (then /admin at http://$(hostname -I | awk '{print $1}'):$PORT/admin)."
echo "   token: $APP/admin-token   logs: journalctl -u soluna-agent -u soluna-node -f   pin to a server: SERVER=ws://host:8900"
[ -n "${NEED_REBOOT:-}" ] && echo "⚠ config.txt changed (DAC overlay / hardware watchdog) — run: sudo reboot"
exit 0
