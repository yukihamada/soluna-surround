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
#   DEVICE  sounddevice name/index (default: auto = USB > GPIO I2S DAC > built-in)
#   DAC     GPIO I2S DAC overlay to enable in config.txt, e.g. hifiberry-dac (PCM5102A/MAX98357A
#           boards without EEPROM), hifiberry-dacplus (PCM5122), iqaudio-dacplus. Needs a reboot.
set -euo pipefail
SERVER="${SERVER:-wss://soluna-sound.fly.dev}"; CH="${CH:-festival}"; ZONE="${ZONE:-A}"
POS="${POS:-C}"; GAIN_DB="${GAIN_DB:-0}"; DEVICE="${DEVICE:-}"
APP=/opt/soluna; USER_NAME="${SUDO_USER:-$(id -un)}"
echo "▶ SOLUNA node install → $APP  (server=$SERVER ch=$CH zone=$ZONE pos=$POS)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-numpy python3-websockets python3-pip libportaudio2 ffmpeg alsa-utils curl >/dev/null
# sounddevice is not packaged everywhere → pip (break-system-packages is fine on a dedicated node)
python3 -c "import sounddevice" 2>/dev/null || sudo pip3 install -q --break-system-packages sounddevice
sudo mkdir -p "$APP" && sudo chown -R "$USER_NAME" "$APP"   # -R: play.py may be root-owned from a cloud-init install
curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/play.py -o "$APP/play.py"
chmod +x "$APP/play.py"
# Output device: play.py --device auto prefers a USB DAC and falls back to the default card.
DEV_ARG="--device ${DEVICE:-auto}"
# GPIO I2S DAC: enable the overlay once (idempotent). Takes effect after reboot.
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
if [ -n "${DAC:-}" ] && [ -f "$CFG" ] && ! grep -q "^dtoverlay=${DAC}\b" "$CFG"; then
  echo "dtoverlay=${DAC}" | sudo tee -a "$CFG" >/dev/null
  echo "   I2S DAC overlay added: dtoverlay=${DAC} (reboot to activate)"; NEED_REBOOT=1
fi
echo "   audio device: ${DEVICE:-auto (USB DAC if present)}"
# Plug-and-play: when a USB sound card appears, restart the node so it moves to the DAC.
sudo tee /etc/udev/rules.d/90-soluna-usb-audio.rules >/dev/null <<'RULE'
ACTION=="add", SUBSYSTEM=="sound", KERNEL=="card*", ENV{ID_BUS}=="usb", RUN+="/bin/systemctl --no-block restart soluna-node.service"
RULE
sudo udevadm control --reload
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
[ -n "${NEED_REBOOT:-}" ] && echo "⚠ DAC overlay was added — run: sudo reboot  (node will pick the I2S DAC automatically after boot)"
