#!/usr/bin/env bash
# SOLUNA Sound — one-shot speaker-node install for Raspberry Pi (Pi 4 / Zero 2W, Raspberry Pi OS).
# Installs deps, drops play.py, and registers a systemd service that survives reboots and WiFi drops.
#
#   curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh \
#     | SERVER=wss://soluna-sound.fly.dev ZONE=B POS=C bash
#
#   SERVER  ws(s)://host[:port]   sync server (LAN FOH Mac in production, fly.dev for demos)
#   CH      channel (default festival)      ZONE  zone letter (default A)
#   POS     L|C|R (default C)               GAIN_DB node level trim (default 0)
#   DEVICE  sounddevice name/index (default: auto = USB audio if present)
set -euo pipefail
SERVER="${SERVER:-wss://soluna-sound.fly.dev}"; CH="${CH:-festival}"; ZONE="${ZONE:-A}"
POS="${POS:-C}"; GAIN_DB="${GAIN_DB:-0}"; DEVICE="${DEVICE:-}"
APP=/opt/soluna; USER_NAME="${SUDO_USER:-$(id -un)}"
echo "▶ SOLUNA node install → $APP  (server=$SERVER ch=$CH zone=$ZONE pos=$POS)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-numpy python3-websockets python3-pip libportaudio2 ffmpeg alsa-utils curl >/dev/null
# sounddevice is not packaged everywhere → pip (break-system-packages is fine on a dedicated node)
python3 -c "import sounddevice" 2>/dev/null || sudo pip3 install -q --break-system-packages sounddevice
sudo mkdir -p "$APP" && sudo chown "$USER_NAME" "$APP"
curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/play.py -o "$APP/play.py"
chmod +x "$APP/play.py"
# USB audio → default ALSA card if present (Pi 4 headphone jack is noisy; USB DAC is the intended path)
if [ -z "$DEVICE" ]; then
  DEVICE=$(python3 - <<'PY'
import sounddevice as sd
for i, d in enumerate(sd.query_devices()):
    if d["max_output_channels"] >= 2 and "usb" in d["name"].lower():
        print(i); break
PY
  ) || true
fi
DEV_ARG=""; [ -n "$DEVICE" ] && DEV_ARG="--device $DEVICE"
echo "   audio device: ${DEVICE:-system default}"
sudo tee /etc/systemd/system/soluna-node.service >/dev/null <<UNIT
[Unit]
Description=SOLUNA Sound speaker node
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $APP/play.py $POS --server $SERVER --ch $CH --zone $ZONE --gain-db $GAIN_DB $DEV_ARG
Restart=always
RestartSec=3
Nice=-10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now soluna-node
sleep 3
sudo systemctl --no-pager status soluna-node | head -12
echo "✅ node running. logs: journalctl -u soluna-node -f   |  change zone: edit /etc/systemd/system/soluna-node.service"
