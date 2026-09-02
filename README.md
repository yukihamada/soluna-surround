# SOLUNA Sound

**The venue is the speaker. The crowd is the light show.**

SOLUNA Sound turns every phone, laptop, projector and cheap speaker in a space into
one phase-aligned system for **sound, light and video**. No app installs — one link,
one tap, and you're inside it. Verified at **10,000 concurrent devices**.

<p align="center">
  <img src="media/how-it-works.svg" width="760"
       alt="Animated diagram: the stage wavefront expands at 343 m/s and each grid node fires exactly as the wavefront passes it">
</p>

<p align="center">
  <a href="https://soluna-sound.fly.dev/?zone=A"><b>▶ Live demo</b></a> ·
  <a href="https://soluna-sound.fly.dev/dj">DJ from your device</a> ·
  <a href="https://soluna-sound.fly.dev/admin">FOH console</a> ·
  <a href="https://soluna-sound.fly.dev/screen">Projector screen</a>
  <br><sub>MIT · Python server (aiohttp, ~500 lines), zero-dependency web clients · ja/en · PWA</sub>
</p>

## Try it in 60 seconds

```bash
git clone https://github.com/yukihamada/soluna-surround && cd soluna-surround
pip install aiohttp && python3 demo.py
```

Open the printed URL on **two phones**, tap ▶ on both — music, lights and (if you
fire one) video lock together. That's the whole product at living-room scale; the
same stack runs a festival.

---

## Architecture

```
                       ┌─────────────────────────────┐
   DJ (any browser) ──▶│        SYNC SERVER          │◀── FOH console /admin
   /dj mic·line·file   │  one aiohttp process        │    zones · align · cues
   source.py --input   │  = single clock authority   │    lights · geo · upload
                       └──────────┬──────────────────┘
                 WebSocket /audio │  JSON control + SL2 binary PCM
        ┌─────────────────┬──────┴────────┬──────────────────┐
        ▼                 ▼               ▼                  ▼
   audience phones   speaker nodes   /screen (Mac+HDMI)   Koe iOS app
   PWA, no install   play.py on Pi   projector / LED      background audio,
   sound+light+video  ±1–3ms wired    wall, same clock     lock-screen safe
```

**One server process is the single source of time.** Everything else — 10,000
phones, Raspberry Pi speakers, a projector — independently synchronizes its own
clock to it and schedules media locally. Nothing streams in lockstep; devices
*agree on when*, then play on their own. That's why it scales.

### How the clock sync works

1. Every client pings: `{t:"ping", c:<local monotonic ms>}` → server replies
   `{t:"pong", c, s:<server epoch ms>}`.
2. Client computes `offset = s + rtt/2 − now` per sample, keeps a 30-sample
   window, and uses the **median of the 3 lowest-RTT samples** (robust against
   asymmetric routes). Pings run at 500 ms until converged, then relax to 3 s.
3. `serverNow() = localMonotonic + offset`. On a LAN this lands within ±1–3 ms;
   over LTE typically ±10–20 ms.

### How audio plays in phase (CUE mode)

- The track is **pre-distributed** (`preload`): every device fetches and decodes it
  before showtime, so FIRE causes zero download burst.
- FIRE broadcasts only `{url, at}` where `at` is a server-epoch instant for
  sample 0. Each device schedules `AudioBufferSource.start()` at
  `at + delay − outputLatency` mapped through its clock offset:
  - `delay = distance/343 + 15 ms` — places this device's sound exactly on the
    stage wavefront (the 15 ms Haas offset keeps the image on stage);
  - `outputLatency` — the device's own hardware buffer (`ctx.outputLatency`),
    which differs 10–30 ms between phone models and would otherwise smear the sync.
- A device that joins mid-track computes its position from the same clock and
  starts *inside* the track, already in phase. Loops re-derive position modulo
  duration.
- **LIVE mode** (DJ/mic): SL2 binary frames (22-byte header + int16 PCM) carry a
  `playAt` driven by a sample counter, never by arrival time — network jitter
  cannot bend the timeline; a late frame is dropped, never played out of phase.
  Pipeline lead is configurable down to 80 ms for blending into a house PA.

### How the light show needs zero bandwidth

`/api/light` broadcasts only *pattern + colors + epoch*. Every device computes its
own color each frame from `(zone position, synced time)` — so a wave literally
sweeps across the crowd, and `audio` mode pulses to an RMS envelope the device
computed **locally from the already-downloaded track** (onset detection: a hit
flashes color 2 at full brightness). Strobe is capped at 3 Hz for photosensitivity.
Patterns: `solid · pulse · beat · wave · plasma · strobe · audio`.

### How video stays in sync

Video is a cue with a `video` url: pre-fetched to a blob, then a per-frame loop
compares `video.currentTime` against the synced clock — >0.5 s off hard-seeks,
smaller drift is absorbed by ±6% `playbackRate`. Measured: 23 ms playback drift,
2 ms on mid-track loop join. Pair it with an audio cue (extract the soundtrack to
mp3) and the video mutes itself: sample-accurate audio + ≤50 ms video.

### How zones disappear (GPS auto-delay)

FOH taps "use my location" once at the stage (`/api/geo`). Each phone then
measures its own GPS distance and computes its delay **continuously**
(`d/343 + 15 ms`) — median of the last 5 accepted fixes, re-syncing mid-track only
when moved >3.5 m ( =10 ms) and at most every 8 s, so playback never stutters.
Zone flags remain the fallback when location is denied; a ±50 ms per-device FINE
TRIM slider nulls any residue by ear.

### Wire format (SL2)

```
header (22 bytes, little-endian):
  magic "SL2" · ver u8 · nchan u8 · pad u8 · seq u32 · nsamp u32 · playAt f64
payload: interleaved int16 PCM
```

---

## The three faces

| Surface | Who | What |
|---|---|---|
| `/?zone=B` | audience | breathing gold orb, one ▶, everything else hidden behind "details". GPS auto-delay, fine trim, PWA-installable |
| `/dj` | performer | any device becomes the venue source: mic/line (music-grade, EC off), input-device picker, file playback, PA-fusion toggle, ON AIR glow, token auth |
| `/admin` | engineer | zones editor (meters in → live delays out), ±1 ms PA alignment, PRELOAD → FIRE cue flow with asset picker & upload, light-show VJ panel, stage-geo, per-zone device counts |
| `/screen` | projector | same client, one click: UI vanishes, cursor hides, video covers — a MacBook + HDMI is a stage screen on the same clock |

## API

| Endpoint | Description |
|---|---|
| `WS /audio?role=listen&ch=&zone=` | listener (JSON control + SL2 binary) |
| `WS /audio?role=push&ch=&token=` | source (hello `{map,sr,lead}` + SL2 frames); token required when `SOLUNA_DJ_TOKEN` is set |
| `POST /api/cue` | `{url?, video?, lead\|at, gain, loop}` · `{preload:true}` · `{stop}` |
| `POST /api/light` | `{pattern, colors, bpm, speed, brightness}` / `{stop}` |
| `POST /api/zones` | `{zones_m:{A:0,B:15,…}}` measured meters → live delays |
| `POST /api/align` | `{base_ms}` global trim vs house PA |
| `POST /api/geo` | `{lat,lng}` stage location for GPS auto-delay |
| `POST /api/upload?name=` | raw-body track/video upload → `/assets/<name>` |
| `GET /api/assets` · `GET /status` | asset list · listeners/zones/cue/light state |

Admin endpoints take `x-soluna-admin` (env `SOLUNA_ADMIN`). Broadcasts are
parallel with per-socket timeouts — one dying phone can't stall the crowd.

## Verified

| Test | Result |
|---|---|
| Protocol suite | 39/39 PASS (clock 0.2 ms, identical cue epochs, mid-join, geo/align/zones live re-broadcast, auth) |
| **10,000 concurrent devices** | 9,999/10,000 connected in 17 s, **cue reached 100%**, identical epochs, 791 ms delivery spread vs 8 s lead, median RTT under load 29 ms |
| Video sync | 23 ms drift; 2 ms mid-track loop join |
| Production E2E | join → sync → light → GPS auto-delay, headless browser vs the live deploy |
| **Real devices** | two iPhones, ears-on: music, light show and timecode video locked — then a real track with onset-flash lighting |

Honest limits: a 10k single-source test hits home-router NAT limits (~3k) — real
crowds are 10k distinct IPs, and show-day runs the server on the FOH laptop (LAN)
anyway. iOS Safari stops web audio on lock screen (the native Koe integration
doesn't); LTE sync (±10–20 ms) is for effects — wired nodes carry the main PA duty.

## Deploy

```bash
SOLUNA_ADMIN=<secret> SOLUNA_DJ_TOKEN=<secret> PORT=8900 python3 server.py
```

Included `fly.toml` ships `connections` concurrency (20k hard) — the default
few-hundred cap silently rejects a crowd. Uploaded assets live on the container FS:
re-upload after a redeploy.

## Roadmap

- **Native iOS** — shipped as the Koe app's "Fest" mode (background audio,
  sample-accurate `AVAudioTime`, zone-flag QR deep links `koe://fest?…`); LED-torch
  sync next. Web/Android enter via `koe.live/fest`.
- **Acoustic auto-calibration** — a stage chirp measured by the mic: true acoustic
  distance, no GPS, no tape measure.
- **Crowd imaging** — per-device pixel coordinates so 10,000 screens can carry
  pictures, not just waves.

---

## 日本語 — 仕組みのまとめ

**1つのサーバプロセスが「時刻の権威」**。1万台のスマホもPiスピーカーもプロジェクターも、
それぞれが自分の時計をサーバに合わせ(最小RTT側3点の中央値・LAN±1〜3ms/LTE±10〜20ms)、
メディアは各端末がローカルで予約再生する。「同時に流す」のではなく「いつ鳴らすかに合意する」
から無限にスケールする。

- **音(CUE)**: 音源は事前配布(PRELOAD)。FIREは`{url, at}`だけを配り、各端末が
  `at + 距離/343+15ms − 自分の出力レイテンシ` に予約。途中参加は曲中位置から合流
- **音(LIVE)**: DJの音はSL2バイナリで、playAtはサンプルカウンタ駆動=ネットワークの揺れで
  タイムラインが曲がらない。遅延80msまで詰められ既存ハウスPAと融合できる
- **光**: 色は配信しない。「パターン+基準時刻」から各端末が自分の位置で色を計算=帯域ゼロで
  1万台の波。audioは端末内で音源の包絡を解析しヒットでフラッシュ
- **映像**: currentTimeを時計に照らして追従(実測ズレ23ms)。音声と併用時は映像ミュート=音は
  サンプル精度
- **ゾーン不要**: FOHがステージ位置を1回登録→各端末がGPS距離から遅延を連続計算・移動追従
- **実証**: 1万台同時接続でキュー到達100%・開始時刻完全一致(ばらつき791ms/猶予8秒)、
  実機iPhone2台での実耳テスト済み。本番会場ではサーバを現地Mac(LAN)で運用する
