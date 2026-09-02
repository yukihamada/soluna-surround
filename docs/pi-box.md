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
