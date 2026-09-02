#!/usr/bin/env bash
# SOLUNA Sound — flash a microSD into a headless speaker node (macOS, Raspberry Pi Imager CLI).
# Result: Pi boots, SSH on (user pi), installs the SOLUNA box (agent + node + standby server) on
# first boot — zero config: it finds the server on the LAN or becomes one (docs/pi-box.md).
#
#   tools/pi-flash.sh <image.img> <disk e.g. /dev/disk4> HOSTNAME=soluna-box-1 \
#       [WIFI_SSID="venue" WIFI_PSK="…"]          # optional upstream Wi-Fi (internet for the install)
#       [SERVER=wss://…  ZONE=B POS=C]            # optional: pin to one server / preset zone
#       [WIFI_COUNTRY=JP] [PI_PASS=raspberry] [AP_PSK=…]
# Every card also gets the boxes' own Wi-Fi "SOLUNA" (client side) with a shared PSK read/generated
# at ~/.config/soluna-sound/ap.psk, and the same PSK as /etc/soluna/ap.psk (used when this box is the
# server and raises the AP). The PSK never enters the repo.
set -euo pipefail
# tools/pi-flash.sh --customize-only <disk> KEY=VAL…   → skip writing, only configure bootfs
CUSTOMIZE_ONLY=0; if [ "${1:-}" = "--customize-only" ]; then CUSTOMIZE_ONLY=1; shift; IMG=""; else IMG="${1:?image.img}"; shift; fi
DISK="${1:?/dev/diskN}"; shift
for kv in "$@"; do export "$kv"; done
WIFI_SSID="${WIFI_SSID:-}"; WIFI_PSK="${WIFI_PSK:-}"
HOSTNAME="${HOSTNAME:-soluna-box}"; SERVER="${SERVER:-auto}"
ZONE="${ZONE:-}"; POS="${POS:-}"; CH="${CH:-festival}"; WIFI_COUNTRY="${WIFI_COUNTRY:-JP}"; PI_PASS="${PI_PASS:-raspberry}"
AP_SSID="${AP_SSID:-SOLUNA}"
# shared PSK for the boxes' own Wi-Fi (generated once per flashing laptop, never committed)
AP_PSK_FILE="$HOME/.config/soluna-sound/ap.psk"
if [ -z "${AP_PSK:-}" ]; then
  if [ -s "$AP_PSK_FILE" ]; then AP_PSK=$(tr -d '\n' < "$AP_PSK_FILE")
  else mkdir -p "$(dirname "$AP_PSK_FILE")"; AP_PSK=$(LC_ALL=C tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' < /dev/urandom | head -c 12)
       (umask 077; echo "$AP_PSK" > "$AP_PSK_FILE"); echo "   generated boxes' Wi-Fi PSK → $AP_PSK_FILE"; fi
fi
IMAGER="/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
[ -x "$IMAGER" ] || { echo "Raspberry Pi Imager.app not found"; exit 1; }
echo "▶ target $DISK:"; diskutil info "$DISK" | grep -E "Device / Media Name|Disk Size|Removable|Protocol" || true
diskutil list "$DISK" | grep -qiE "SD|Removable|external" || echo "⚠ not obviously removable — check the disk above"
if [ "$CUSTOMIZE_ONLY" = 0 ]; then
read -r -p "ERASE $DISK and write $IMG? [yes/N] " a; [ "$a" = yes ] || exit 1
diskutil unmountDisk force "$DISK"   # 消去対象: 他プロセスが掴んでいても外す
# Prefer raw dd (needs sudo; rdisk = unbuffered, ~3x faster). Fall back to Imager CLI.
RDISK="${DISK/\/dev\/disk//dev/rdisk}"
if sudo -n true 2>/dev/null; then
  echo "   writing with dd → $RDISK"
  sudo dd if="$IMG" of="$RDISK" bs=4m status=progress
  sync
else
  echo "   (no sudo cache: run  sudo -v  first for the fast dd path) → Raspberry Pi Imager CLI"
  "$IMAGER" --cli --disable-verify "$IMG" "$DISK"
fi
sleep 3
fi
# bootfs auto-mounts as /Volumes/bootfs on macOS
BOOT=/Volumes/bootfs; for i in $(seq 1 20); do [ -d "$BOOT" ] && break; sleep 1; diskutil mountDisk "$DISK" >/dev/null 2>&1 || true; done
[ -d "$BOOT" ] || { echo "bootfs not mounted"; exit 1; }
psk_hash(){ python3 - "$1" "$2" <<'PY'
import hashlib, sys
print(hashlib.pbkdf2_hmac('sha1', sys.argv[2].encode(), sys.argv[1].encode(), 4096, 32).hex())
PY
}
PSK_HASH=""; [ -n "$WIFI_SSID" ] && PSK_HASH=$(psk_hash "$WIFI_SSID" "$WIFI_PSK")
AP_PSK_HASH=$(psk_hash "$AP_SSID" "$AP_PSK")
# NB: macOS libc crypt() has no SHA-512 → python's crypt returns a 13-char DES junk hash that Linux
# rejects. Use openssl (OpenSSL 3 supports -6) and validate the shape.
USER_HASH=$(openssl passwd -6 "$PI_PASS" 2>/dev/null || true)
case "$USER_HASH" in \$6\$*) ;; *) echo "✘ could not build a SHA-512 password hash (need OpenSSL 3: brew install openssl)"; exit 1;; esac
# Bookworm headless config (same file Imager writes): hostname / user / wlan / ssh
cat > "$BOOT/custom.toml" <<TOML
config_version = 1
[system]
hostname = "$HOSTNAME"
[user]
name = "pi"
password = "$USER_HASH"
password_encrypted = true
[ssh]
enabled = true
password_authentication = true
[wlan]
ssid = "${WIFI_SSID:-$AP_SSID}"
password = "${PSK_HASH:-$AP_PSK_HASH}"
password_encrypted = true
hidden = false
country = "$WIFI_COUNTRY"
[locale]
keymap = "us"
timezone = "Asia/Tokyo"
TOML
touch "$BOOT/ssh"
# ---- Trixie (2025-10+) images ignore custom.toml and use cloud-init instead ----------------
# Write both: user-data (user/ssh/hostname + installer as runcmd) and network-config (Wi-Fi).
cat > "$BOOT/user-data" <<CLOUD
#cloud-config
hostname: $HOSTNAME
manage_etc_hosts: true
timezone: Asia/Tokyo
keyboard:
  layout: us
ssh_pwauth: true
users:
- name: pi
  groups: users,adm,dialout,audio,netdev,video,plugdev,games,input,gpio,spi,i2c,render,sudo
  shell: /bin/bash
  lock_passwd: false
  passwd: "$USER_HASH"
  sudo: ALL=(ALL) NOPASSWD:ALL
# belt and braces: also set the password via chpasswd (works for an already-existing user on re-run)
chpasswd:
  expire: false
  users:
  - name: pi
    password: "$USER_HASH"
    type: hash
write_files:
- path: /etc/soluna/ap.psk
  permissions: "0600"
  content: "$AP_PSK"
- path: /usr/local/bin/soluna-diag
  permissions: "0755"
  content: |
    #!/bin/bash
    # Dump network state to the FAT boot partition so it can be read on any laptop.
    O=/boot/firmware/soluna-diag.txt
    { date; echo "== nmcli dev"; nmcli dev; echo "== wifi list"; nmcli -f SSID,CHAN,SIGNAL,SECURITY dev wifi list 2>&1 | head -20;
      echo "== connections"; nmcli -f NAME,TYPE,DEVICE con show 2>&1; echo "== ip"; ip -br a;
      echo "== rfkill"; rfkill list 2>&1; echo "== NM log"; journalctl -u NetworkManager -b --no-pager 2>&1 | tail -40;
      echo "== wpa/iwd"; journalctl -b --no-pager 2>&1 | grep -iE "wpa_supplicant|iwd|wlan0" | tail -30;
      echo "== cloud-init"; cloud-init status --long 2>&1; tail -30 /var/log/cloud-init.log 2>&1; } > "\$O" 2>&1
runcmd:
- [ bash, -c, "raspi-config nonint do_wifi_country $WIFI_COUNTRY || true; rfkill unblock wifi || true; sleep 20; /usr/local/bin/soluna-diag" ]
- [ bash, -c, "for i in \$(seq 1 60); do curl -fsS https://raw.githubusercontent.com >/dev/null 2>&1 && break; sleep 5; done; curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh | SERVER='$SERVER' ZONE='$ZONE' POS='$POS' CH='$CH' SUDO_USER=pi bash > /var/log/soluna-install.log 2>&1" ]
CLOUD
cat > "$BOOT/network-config" <<NET
network:
  version: 2
  wifis:
    renderer: NetworkManager
    wlan0:
      dhcp4: true
      optional: true
      access-points:
$( [ -n "$WIFI_SSID" ] && printf '        "%s":\n          password: "%s"\n' "$WIFI_SSID" "$PSK_HASH" )
        "$AP_SSID":
          password: "$AP_PSK_HASH"
NET
echo "instance-id: $HOSTNAME-$(date +%s)" > "$BOOT/meta-data"
# USB gadget mode: plug the Pi's USB-C into a laptop → "RNDIS/Ethernet Gadget" NIC → ssh over USB,
# no Wi-Fi needed for first contact / diagnosis. (Pi 4/Zero 2W; harmless on others.)
grep -q "^dtoverlay=dwc2" "$BOOT/config.txt" || printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$BOOT/config.txt"
grep -q "modules-load=dwc2" "$BOOT/cmdline.txt" || sed -i '' 's|rootwait|rootwait modules-load=dwc2,g_ether|' "$BOOT/cmdline.txt"
# First-boot installer: waits for network, runs pi-setup.sh once, removes itself.
cat > "$BOOT/soluna-install.sh" <<SH
#!/bin/bash
set -e
export SERVER="$SERVER" ZONE="$ZONE" POS="$POS" CH="$CH"
for i in \$(seq 1 60); do curl -fsS https://raw.githubusercontent.com >/dev/null 2>&1 && break; sleep 5; done
curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh | SUDO_USER=pi bash
systemctl disable soluna-install.service
SH
chmod +x "$BOOT/soluna-install.sh"
cat > "$BOOT/soluna-install.service" <<UNIT
[Unit]
Description=SOLUNA node first-boot installer
After=network-online.target
Wants=network-online.target
ConditionPathExists=/boot/firmware/soluna-install.sh
[Service]
Type=oneshot
ExecStart=/bin/bash /boot/firmware/soluna-install.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
# Hook the unit in via the kernel cmdline first-run mechanism (Imager's own trick):
# firstrun.sh runs once before login, copies the unit into place, enables it, then reboots.
cat > "$BOOT/firstrun.sh" <<'SH'
#!/bin/bash
set +e
if [ ! -d /etc/cloud ]; then   # Bookworm: no cloud-init → use our oneshot installer
cp /boot/firmware/soluna-install.service /etc/systemd/system/soluna-install.service
systemctl enable soluna-install.service
fi
rm -f /boot/firmware/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt
exit 0
SH
chmod +x "$BOOT/firstrun.sh"
CMD=$(cat "$BOOT/cmdline.txt")
echo "${CMD% } systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target" > "$BOOT/cmdline.txt"
sync; if [ "${NO_EJECT:-0}" = 1 ]; then echo "(NO_EJECT=1: left mounted for inspection)"; else diskutil eject "$DISK"; fi
echo "✅ flashed. Insert into the Pi, power on (DAC attached). Zero config: it joins a server or becomes one."
echo "   ~3-5 min later: ssh pi@$HOSTNAME.local  (pass: $PI_PASS)  → journalctl -u soluna-agent -u soluna-node -f"
echo "   FOH: /admin NODES → assign its zone.   boxes' Wi-Fi: SSID $AP_SSID (psk in $AP_PSK_FILE)"
