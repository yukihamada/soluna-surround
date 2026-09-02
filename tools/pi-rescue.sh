#!/usr/bin/env bash
# SOLUNA box rescue — a box that turned itself into a Wi-Fi AP ("SOLUNA") and can't be reached.
# Pull its microSD, plug it into this Mac, run:  tools/pi-rescue.sh [/Volumes/bootfs]
# Writes a one-shot cloud-init (new instance-id) that on next boot:
#   • stops the AP and forbids it from autoconnecting (agent decides again after the grace period)
#   • sets the AP password to the known default (solunasound) so it is never a stranger again
#   • brings the saved upstream Wi-Fi back up (tethering / venue LAN profile already on the card)
#   • updates SOLUNA to the latest release (pi-setup.sh) once online
# Nothing else on the card is touched. Put the card back, power on, wait ~2 min.
set -euo pipefail
BOOT="${1:-/Volumes/bootfs}"
[ -f "$BOOT/config.txt" ] || { echo "🔴 $BOOT is not a Raspberry Pi boot partition (config.txt missing)"; exit 1; }
AP_PSK="${AP_PSK:-solunasound}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# bundle the local checkout so the box updates even without internet (and never from a stale CDN)
tar -czf "$BOOT/soluna-src.tgz" -C "$REPO" --exclude .git --exclude '*.log' --exclude __pycache__ --exclude media --exclude .claude . 2>/dev/null
echo "   bundled $(du -h "$BOOT/soluna-src.tgz" | cut -f1) of source onto the card"
UPSTREAM="${UPSTREAM:-}"    # optional: nmcli profile name to bring up (default: any saved non-AP Wi-Fi)
ID="rescue-$(date +%Y%m%d%H%M%S)"
cat > "$BOOT/meta-data" <<META
instance-id: $ID
local-hostname: $(grep -m1 '^hostname:' "$BOOT/user-data" 2>/dev/null | awk '{print $2}' || echo soluna-box)
META
cat > "$BOOT/user-data" <<'UD'
#cloud-config
runcmd:
  - [ bash, -c, "nmcli con modify soluna-ap connection.autoconnect no || true" ]
  - [ bash, -c, "nmcli con down soluna-ap || true" ]
  - [ bash, -c, "nft delete table inet soluna_ap 2>/dev/null || true" ]
  - [ bash, -c, "mkdir -p /etc/soluna && printf '%s\n' '__AP_PSK__' > /etc/soluna/ap.psk && chmod 600 /etc/soluna/ap.psk" ]
  - [ bash, -c, "touch /etc/soluna/agent.env; grep -q '^SOLUNA_AP_PSK=' /etc/soluna/agent.env && sed -i 's/^SOLUNA_AP_PSK=.*/SOLUNA_AP_PSK=__AP_PSK__/' /etc/soluna/agent.env || echo SOLUNA_AP_PSK=__AP_PSK__ >> /etc/soluna/agent.env" ]
  - [ bash, -c, "grep -q '^SOLUNA_AP_SECURITY=' /etc/soluna/agent.env || echo SOLUNA_AP_SECURITY=open >> /etc/soluna/agent.env" ]
  - [ bash, -c, "P='__UPSTREAM__'; if [ -z \"$P\" ]; then P=$(nmcli -t -f NAME,TYPE con show | awk -F: '$2 ~ /wireless|wifi/ && $1 != \"soluna-ap\" {print $1; exit}'); fi; [ -n \"$P\" ] && nmcli con up \"$P\" || true" ]
  - [ bash, -c, "mkdir -p /home/pi/soluna-src && tar -xzf /boot/firmware/soluna-src.tgz -C /home/pi/soluna-src && chown -R pi:pi /home/pi/soluna-src && SRC=/home/pi/soluna-src SUDO_USER=pi bash /home/pi/soluna-src/tools/pi-setup.sh > /var/log/soluna-rescue.log 2>&1 || true" ]
  - [ bash, -c, "systemctl restart soluna-agent || true" ]
UD
sed -i '' -e "s|__AP_PSK__|$AP_PSK|g" -e "s|__UPSTREAM__|$UPSTREAM|g" "$BOOT/user-data" 2>/dev/null || sed -i -e "s|__AP_PSK__|$AP_PSK|g" -e "s|__UPSTREAM__|$UPSTREAM|g" "$BOOT/user-data"
sync
echo "✅ rescue written to $BOOT (instance-id $ID). Eject, put the card back, power on, wait ~2 min."
echo "   Then the box is back on its saved Wi-Fi; AP password (if it ever raises one again) = $AP_PSK"
