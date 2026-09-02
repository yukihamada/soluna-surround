# Show control: OSC in · timecode · Art-Net / sACN out

SOLUNA Sound already runs a show from `/admin`. This page is for the festival that
already *has* a show system — a playback rig on timecode, a lighting desk, QLab on the
FOH Mac — and wants SOLUNA to be one more thing that listens to it, not another operator.

Everything here lives in `server.py` + `showctl.py` (no extra dependencies) and is **off by
default**. Turn each bridge on with an environment variable. Nothing here is exposed by
the cloud demo.

| Bridge | Enable | Direction | Transport |
|---|---|---|---|
| OSC | `SOLUNA_OSC_PORT=9000` | in (console → SOLUNA) | UDP |
| Timecode | always (HTTP) · OSC `/soluna/tc` · `ltc.py` | in | HTTP / UDP / audio (LTC) |
| Art-Net | `SOLUNA_ARTNET=<ip>[:universe]` | out (SOLUNA → fixtures) | UDP 6454 |
| sACN (E1.31) | `SOLUNA_SACN=<ip>[:universe]` | out | UDP 5568 |

---

## OSC in

Any device that can send an OSC message over UDP can fire SOLUNA. Messages call
**exactly the same internal functions as the HTTP API**, so a cue from QLab and a cue from
`/admin` are indistinguishable to the phones. Bundles are supported: a bundle with a
*future* NTP timetag makes that timetag the cue's `at` (sample-accurate scheduling from
the console's clock — see the timing note below).

| Address | Arguments | Same as |
|---|---|---|
| `/soluna/cue` | `url:s [lead:f] [gain:f]` | `POST /api/cue {url, lead, gain}` |
| `/soluna/preload` | `url:s` | `POST /api/cue {url, preload:true}` |
| `/soluna/stop` | — | `POST /api/cue {stop:true}` |
| `/soluna/go` | — | `POST /api/show {next:true}` (fires the next setlist step) |
| `/soluna/show/goto` | `i:i` (1-based) | `POST /api/show {goto:i}` |
| `/soluna/light` | `pattern:s [color1:s] [color2:s] [bpm:f]` | `POST /api/light` |
| `/soluna/light/stop` | — | `POST /api/light {stop:true}` |
| `/soluna/align` | `ms:f` | `POST /api/align {base_ms}` |
| `/soluna/zone` | `name:s delay_ms:f` | one zone of `POST /api/zones` |
| `/soluna/tc` | `"HH:MM:SS:FF":s [fps:f]` | `POST /api/timecode` |

- **Channel**: everything targets channel `festival`. Append a final string argument
  `ch=<name>` to address another channel (`/soluna/cue "/assets/a.mp3" 3.0 "ch=stage2"`).
- **Types**: `i f s b T F` are parsed; numbers are accepted as either int or float.
- **Timing**: a bare message fires after `lead` seconds (default 3 s, the same as the
  console's FIRE). A bundle with a future timetag fires *at that wall-clock instant*, so the
  console and the SOLUNA server must share NTP (they do on any venue LAN with a time
  source; the offset shows up directly as a cue offset — check once with a click).
- **Security**: OSC has no authentication. It is meant for the show-control VLAN. The
  cloud demo never sets `SOLUNA_OSC_PORT`. If your control network is shared, firewall
  the port to the console's IP.

### Console recipes

**QLab 5** — Network cue → OSC message, destination = SOLUNA server IP : `SOLUNA_OSC_PORT`:
```
/soluna/preload "/assets/open.mp3"                (in the pre-show block)
/soluna/cue "/assets/open.mp3" 3.0 1.0            (GO = SOLUNA fires 3 s later)
/soluna/light "wave" "#d4af37" "#7fc9a2" 96.0
/soluna/stop
```
Tip: keep `lead` ≥ 2 s over Wi-Fi so every phone's clock offset is settled; on a wired LAN
with Pi nodes 0.5 s is plenty.

**Ableton Live** — Max for Live `udpsend` (or any OSC device): send `/soluna/go` on a
clip's launch, or `/soluna/tc <tc> 30.` every second from the arrangement position to
lock SOLUNA to the set's timeline (then your setlist steps carry `tc` instead of `lead`).

**grandMA3 / grandMA2** — OSC output in *Setup → Network → OSC*: destination SOLUNA IP,
port `SOLUNA_OSC_PORT`. Trigger a macro on the sequence's GO:
`SendOSC "/soluna/light" "pulse" "#ff2a00" "#0a0a40" 128` — or let SOLUNA *follow* the
desk instead via Art-Net (below), which is usually the cleaner split: **the desk owns
light, SOLUNA owns the crowd's phones.**

**ETC Eos** — *Show Control → OSC → OSC TX*: add SOLUNA as a UDP target and use a cue's
*External Links* string, e.g. `/soluna/cue "/assets/drop.mp3" 2.0`.

**Anything else** — `python3 -c "import showctl,socket; s=socket.socket(2,2);
s.sendto(showctl.build_osc_message('/soluna/go'), ('192.168.1.10', 9000))"`.

---

## Timecode

Two calls make SOLUNA timecode-aware:

1. **Anchor**: tell the server *"right now, the show timecode is X"* —
   `POST /api/timecode {"tc":"01:00:00:00","fps":30}` (admin) or OSC `/soluna/tc`.
   The server stores `{epoch, frames, fps, drop}` and persists it. Send it again any time
   (every second is fine); each update re-anchors and follows the playback rig's drift.
2. **Cue by timecode**: `POST /api/cue {"url":"/assets/a.mp3","tc":"01:00:10:00"}` — the
   server converts the target timecode into a server-epoch `at`. Setlist steps accept
   `tc` too, so a whole night can be authored against the show's timeline and fired
   with GO/`/soluna/go`.

Supported rates: 24, 25, 30, 29.97 drop-frame (`fps: 29.97`, or write frames as `;FF`),
59.94 DF, 23.976. Drop-frame counting follows SMPTE 12M (two frames dropped each minute
except every tenth) — `GET /api/timecode` returns the current *estimated* show timecode
so you can eyeball it against the rig's display.

### From LTC (audio timecode)

Most playback rigs already put LTC on a spare output. Feed it to any audio input on the
FOH Mac / Pi and run:

```bash
python3 ltc.py --list                                  # find the input
python3 ltc.py --device 3 --channel 0 \
    --server http://192.168.1.10:8900 --token "$SOLUNA_ADMIN" --ch festival
```

`ltc.py` decodes SMPTE 12M biphase-mark frames (sync word `0x3FFD`, BCD fields, DF flag,
frame rate inferred from the frame length) with numpy only, and POSTs the anchor once a
second. It survives level changes, DC offset and noise; verified round-trip at 24/25/30/
29.97DF including frames split across buffers (`tests/test_showcontrol.py`).
`--wav file.wav` decodes a file for a dry run.

Precision: the anchor is taken at the moment the frame is decoded on the server's clock;
input-buffer latency (typically 10–20 ms) shows up as a constant offset. Null it once with
`/api/align` while listening to a click — the same trim you already do against the house PA.

---

## Art-Net / sACN out (SOLUNA light → real fixtures)

When a SOLUNA light pattern is running, the server can render the *same* pattern to DMX
at 40 Hz and send it as Art-Net (ArtDMX, protocol 14) and/or sACN (E1.31, priority 100):

```bash
SOLUNA_ARTNET=192.168.1.255:0   SOLUNA_DMX_FIXTURES=12  python3 server.py   # broadcast, universe 0
SOLUNA_SACN=239.255.0.1:1                               python3 server.py   # E1.31 multicast, universe 1
```

| Variable | Meaning | Default |
|---|---|---|
| `SOLUNA_ARTNET` | `ip[:universe]` — `.255` broadcasts; universe = Net·SubUni (0–32767) | off |
| `SOLUNA_SACN` | `ip[:universe]` — unicast or E1.31 multicast (`239.255.<hi>.<lo>`) | off |
| `SOLUNA_DMX_FIXTURES` | number of RGB fixtures, patched consecutively | 8 |
| `SOLUNA_DMX_START` | first DMX channel of fixture 1 | 1 |
| `SOLUNA_DMX_CH` | which SOLUNA channel's light to render | `festival` |
| `SOLUNA_ARTNET_PORT` / `SOLUNA_SACN_PORT` | override ports (tests, odd nodes) | 6454 / 5568 |

Mapping: fixture *i* is patched at channels `START + 3i .. +2` (R, G, B) and stands at
position `x = i / (N−1)` across the venue — the same 0..1 axis the phones use for their
zone, so a `wave` really travels from fixture 1 to fixture N in step with the crowd's
screens. Patterns `solid · pulse · beat · wave · plasma · strobe` are ported from
`client.html` unchanged (strobe keeps the 3 Hz photosensitivity cap). `audio` needs the
phone-side envelope analysis and **falls back to `pulse`** on DMX. Stopping the light sends
one blackout frame, then the sender goes quiet so the desk can take the universe back
(set SOLUNA's sACN priority below the desk's if both drive the same universe: the desk
wins by priority; with Art-Net, use HTP/LTP merge on the node or separate universes).

Latency: DMX frames carry the light's *current* colour; there is no zone delay on the
fixture side because fixtures are on stage. Phones in the field see the same instant
delayed by their zone's acoustic delay — intentional: light and sound arrive together.

---

## 日本語 — 既存の卓・制御系につなぐ

- **OSC 入力**(`SOLUNA_OSC_PORT=9000`): QLab / Ableton / grandMA / Eos から UDP で
  `/soluna/cue "/assets/a.mp3" 3.0` のように叩く。HTTP API と同じ内部処理を呼ぶので、
  `/admin` の FIRE と挙動は同一。バンドルの未来 timetag は発火時刻 `at` になる
  (卓とサーバは NTP で揃えること)。認証なし=制御系 VLAN 専用。クラウド版は無効。
- **タイムコード**: 「今この瞬間の番組 TC」を `POST /api/timecode {"tc":"01:00:00:00","fps":30}`
  (または OSC `/soluna/tc`、LTC 音声なら `python3 ltc.py --device N`)で 1 回教えると、
  以後の cue とセットリストの各ステップは `"tc":"01:00:10:00"` で発火時刻を指定できる。
  24/25/30/29.97DF 対応。入力バッファ分の一定オフセットは `/api/align` で一度だけ追い込む。
- **Art-Net / sACN 出力**(`SOLUNA_ARTNET=192.168.1.255:0` / `SOLUNA_SACN=239.255.0.1:1`):
  進行中のライトパターンを 40Hz で DMX に落として会場の灯体へ。灯体 i の位置 = 会場横断
  0..1 なので、wave がスマホの海と同じ向き・同じ速さで灯体を渡る。`audio` は端末側解析が
  要るため DMX 側では `pulse` にフォールバック。stop で消灯 1 回送って沈黙(卓が取り戻せる)。
- **役割分担の推奨**: 光は卓が主(SOLUNA は Art-Net で追従するか、卓が OSC で SOLUNA の
  スマホ演出を叩く)。音のキューは再生卓のタイムコードに乗せる。SOLUNA が新しいオペレータを
  増やさない形が一番安全。
