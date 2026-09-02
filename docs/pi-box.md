# The SOLUNA box — zero-config Raspberry Pi (flash → power → done)

A SOLUNA box is any Raspberry Pi flashed with `tools/pi-flash.sh` (or installed with
`tools/pi-setup.sh`). It needs **no per-box configuration**. Boxes find each other, one of
them becomes the sync server (clock authority), the others become speaker nodes, they watch
each other, restart whatever dies, and a node takes over the show if the server box dies.
A box that finds no network raises its own Wi-Fi so phones and other boxes can join.

## What happens after power-on

```
 boot ──▶ DISCOVER (≈6 s)  mDNS _soluna._tcp + UDP beacon :8901
            │
            ├─ server found ──▶ NODE   point soluna-node at it (±0.5–3 ms on a LAN)
            │                          keep a warm copy of the show every 10 s (standby-state.json)
            │                          watch its /health every 2 s
            │
            └─ none ──▶ ELECTION (3 s)  announce candidacy; best box wins:
                          Ethernet link up  >  longest uptime  >  lowest hostname
                          │
                          └─▶ SERVER  start soluna-server, publish mDNS + beacon,
                                      restore the standby copy if < 10 min old
                                      (cues / zones / light / setlist position resume),
                                      own node → ws://127.0.0.1:8900,
                                      no upstream Wi-Fi? → raise AP "SOLUNA"
```

Every 2 s each box heals itself: own server unhealthy 3× → restart it; node service down →
start it; audio card list changed (USB DAC plugged) → restart the node onto it. The kernel
hardware watchdog reboots a hung box; systemd restarts every service; journald is capped
at 50 MB so the SD card never fills.

Every 5 s each box reports to the server (`POST /api/nodes/report`: host, ip, role, CPU
temp, load, disk, audio device, service states, agent version). **`/admin` → NODES** lists
them; type a zone letter / pick L·C·R / a gain and press ASSIGN — the box applies it live,
persists it (`/opt/soluna/node.json`), and the server re-sends it on every reconnect, so
the assignment survives reflashes of the box's SD card too.

## Failover (server box dies)

| Step | Time |
|---|---|
| nodes miss 5 health checks | ≈10 s |
| re-discover (nothing) → election | ≈6–9 s |
| winner starts server, restores the standby copy, re-broadcasts config / cue / light | ≈2–3 s |
| **takeover total** | **≈15–20 s** |

What survives: zones, alignment, stage geo, active cue and light, setlist position, node
assignments — because the copy is re-imported with `POST /api/state` and re-broadcast.
Cue epochs stay valid: all boxes were synced to the same clock and the new server's clock
is anchored to wall time on boot (boxes without RTC should have NTP or accept a one-time
jump at the switch). Phones reconnect to whatever address they had; on a Pi AP the new
server has a **new IP**, so the phones re-scan the QR (or use the `.local` name where
mDNS works). What does not survive: a **LIVE** PCM stream — the source (`source.py`) has
to reconnect to the new server (it retries by itself if started with the `.local` name).

## Networking

- **Ethernet between boxes** whenever possible (a small PoE switch): ±1–3 ms sync and no
  Wi-Fi contention. The election prefers boxes with a wired link for exactly that reason.
- **Pi AP** (`SOLUNA`, 2.4 GHz for range): the Pi 4's radio holds about **20–30 clients**
  reliably — enough for the other boxes and a few staff phones, not for a crowd. Crowd
  phones ride LTE + the cloud deploy, or venue Wi-Fi (see README "How it scales over the
  air"). The AP password is generated once per box (`/etc/soluna/ap.psk`, 0600); flashing
  from one laptop bakes the same PSK into every card (`tools/pi-flash.sh` → `AP_PSK`) so all
  boxes can join whichever box became the server. The SSID/password are shown only on
  `/admin` (NODES), never printed on flags.
- If the box's `wlan0` is already on an upstream network (venue Wi-Fi, phone tethering),
  no AP is raised — the boxes just use that network.
- `SOLUNA_AP=0` in `/etc/soluna/agent.env` disables the AP; `SOLUNA_AP_BAND=a` picks 5 GHz.

## Forcing roles

- `sudo touch /etc/soluna/force-server` → this box is always the server and never yields
  (`tools/pi-server-setup.sh` writes it). Use it for the FOH box.
- `SERVER=ws://host:8900` at install → node is pinned to that server (`PINNED=1` in
  `/etc/soluna/node.env`); the agent still heals services but does not retarget.
- Two servers on one network (e.g. a forced box joins late): the lower-priority one steps
  down to node automatically.

## Install

```bash
# a) SD card from a Mac — nothing else to type on the Pi
tools/pi-flash.sh raspios.img /dev/disk4 HOSTNAME=soluna-box-1 [WIFI_SSID=… WIFI_PSK=…]
# b) an already-running Pi
curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh | bash
# GPIO I2S DAC boards (PCM5102A/MAX98357A): add DAC=hifiberry-dac, then reboot
```

Logs: `journalctl -u soluna-agent -u soluna-node -u soluna-server -f`. Admin token of a box
that may become the server: `/opt/soluna/admin-token`.

---

## 日本語: SOLUNA box — 焼いて、電源を入れるだけ

- **設定不要**: 起動した箱は6秒ほどLANを聞き、サーバがあればスピーカーノードになり、無ければ
  選挙(有線あり > 稼働時間 > ホスト名)で1台がサーバ(時計の親)になります。
- **お互いを監視**: サーバの /health を2秒ごとに見て、10秒落ちたら再選挙。自分のサービスが
  死んでいれば起動し直す。USB DACを挿せばノードが自動でそちらへ。カーネルが固まれば
  ハードウェアウォッチドッグで再起動。ログはSDを埋めない(50MB上限)。
- **引き継ぎ ≈15〜20秒**: ノードは10秒ごとにショー状態の控えを持つので、新サーバがそれを
  取り込みキュー/ゾーン/ライト/セットリスト位置ごと再配信。LIVE配信だけは送出側の再接続が必要。
- **PiだけでAP**: 上流Wi-Fiが無い箱がサーバになると SSID `SOLUNA` を立てる(2.4GHz)。収容は
  20〜30台=他の箱+スタッフ端末向け。観客はLTE+クラウド、または会場Wi-Fi。SSID/パスワードは
  `/admin` NODESだけに表示(旗には刷らない)。
- **ゾーン割当は /admin から**: NODESの行にゾーン文字/位置/ゲインを入れて ASSIGN。箱に保存され、
  再接続時にもサーバから再送されるので、SDを焼き直しても割当は残ります。
- **箱同士は有線推奨**(小型PoEスイッチ)。選挙も有線の箱を優先します。

## Plug in → a page opens (captive portal + `/setup`)

Join the box's Wi-Fi **SOLUNA** (default password `solunasound`, change it in `/setup`) and the
phone opens the welcome page by itself — the OS connectivity probe (Apple `hotspot-detect`,
Android `generate_204`, Windows NCSI, Firefox) gets our landing instead of "Success", and every
DNS name resolves to the box while the AP is up. Two buttons: **🎧 join the sound** (audience
page) and **⚙ set this box up**.

`/setup` (also at `http://<box>.local:8900/setup` from any network the box is on) covers:

| Section | What you can change | Effect |
|---|---|---|
| Status | — | role, IPs, services, sound card, temperature, uptime, Wi-Fi state, 🔔 test tone (880 Hz through the node's DAC) |
| Role | auto / always server / pinned server URL | writes `/etc/soluna/force-server` or `node.env`, restarts agent+node |
| Speaker | zone, L/R/C, gain dB, output device (list from `aplay -l`), channel | `node.env`, node restart |
| Wi-Fi uplink | scan + join venue Wi-Fi / tethering | `nmcli dev wifi connect` — the AP goes down while an uplink is up |
| Own AP | on/off, SSID, password (8–63), 2.4/5 GHz | `agent.env` + `ap.psk`, agent re-raises the AP |
| Box | hostname, admin token (show / regenerate), restart node/server, update from GitHub, reboot, logs | `sudo -n` via `/etc/sudoers.d/soluna` (only those commands) |

Auth: the admin token, **or** simply being on the box's own AP / localhost
(`SOLUNA_SETUP_OPEN=1`, default on a box — the WPA2 PSK is the key). Set `SETUP_OPEN=0` at
install time to require the token everywhere. Cloud deploys never expose `/setup` (`SOLUNA_BOX` unset).

### AP policy (so a box never strands itself)

A flaky uplink must not turn a box into an unreachable island. The agent therefore:

1. raises the AP only after the uplink has been gone for **120 s** (`SOLUNA_AP_GRACE_S`) when a
   saved upstream Wi-Fi profile exists (immediately if none exists — a box with no known network
   *should* offer its own);
2. marks the AP connection `autoconnect=no`, so after a reboot the saved uplink is tried first;
3. while in AP mode, every **10 min** (`SOLUNA_AP_RETRY_S`) drops the AP for 45 s to see whether
   the uplink is back, and re-raises it if not;
4. uses a **known default password** (`solunasound`, or the fleet PSK baked by `pi-flash.sh`) and
   prints SSID + PSK in the journal and on `/setup` / `/admin` NODES.

Field note (2026-09-02): the first Pi 4 ran a build without rule 1/2/4 — its tethering uplink
dropped for a moment, it became AP "SOLUNA" with a random PSK, and nobody could get in.
Recovery = `tools/pi-rescue.sh`: pull the microSD, run the script against `bootfs`, put it back;
a one-shot cloud-init disables the AP autoconnect, sets the known PSK, re-joins the saved uplink
and updates the box.

### Open Wi-Fi, still safe (no password by default)

The box's AP has **no password by default** — a crew member or a phone should be able to join
in one tap. Safety does not come from the Wi-Fi key; it comes from the box being *closed*:

- **Firewall on the AP interface** (`nft`, or `iptables` fallback): only the box's own web/WS
  ports 80 and 8900 (+ DNS/DHCP/mDNS/beacon UDP) are reachable. **SSH is not**. Nothing is
  forwarded to the uplink — the open Wi-Fi is not a free hotspot and cannot reach the venue LAN.
- **Client isolation** (`ap-isolation`): phones on the AP cannot see each other.
- **Control needs a key anyway**: `/admin` always wants the token. `/setup` is open from the AP
  only for **10 minutes after power-on** (`SOLUNA_SETUP_OPEN=window`, `SOLUNA_SETUP_WINDOW_S`) —
  the person who just plugged the box in is the person allowed to configure it. After that:
  token, or reboot the box to reopen the window. `always` / `never` are available.
- The audience page carries no secrets, and location never leaves the phone.
- Prefer encryption without a password? `security: owe` (Wi-Fi Enhanced Open) in `/setup` — newer
  phones only. Prefer a password? `security: wpa` (default PSK `solunasound`, change it).
