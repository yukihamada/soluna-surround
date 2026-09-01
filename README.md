# SOLUNA Sound

**The venue is the speaker. The crowd is the light show.**

SOLUNA Sound turns every phone, laptop and cheap speaker in a space into one
phase-aligned sound system — and every screen into a pixel of a venue-wide light
show. No app installs. One link, one tap, and you're inside the sound.

<p align="center">
  <img src="media/how-it-works.svg" width="760"
       alt="Animated diagram: the stage wavefront expands at 343 m/s and each grid node fires exactly as the wavefront passes it">
</p>

<p align="center">
  <a href="https://soluna-sound.fly.dev/?zone=A"><b>▶ Live demo</b></a> ·
  <a href="https://soluna-sound.fly.dev/dj">DJ from your device</a> ·
  <a href="https://soluna-sound.fly.dev/admin">FOH console</a>
  <br><sub>MIT license · Python server, zero-dependency web clients · ja/en</sub>
</p>

## Try it in 60 seconds

```bash
git clone https://github.com/yukihamada/soluna-surround && cd soluna-surround
pip install aiohttp && python3 demo.py
```

Open the printed URL on **two phones**, tap ▶ on both — the music and the light
show lock together across devices. That's the whole product, at living-room scale.
The same stack runs a 5,000-person festival.

## Why it sounds better than a big PA

Sound travels at just 343 m/s. A giant stage rig blasts the front row to barely
reach the back. SOLUNA Sound does what festival engineers do with delay towers —
taken to its logical extreme: put many small sources *inside* the crowd and delay
each one by `distance/343 + 15ms`, so every speaker fires **exactly as the stage
wavefront passes it**. The Haas effect keeps the image on stage; the loudness
comes from right next to you. Everyone stands in the sweet spot.

## What's in the box

| | |
|---|---|
| 🔊 **Cue engine** | Audio is pre-fetched; the server broadcasts only *what to play and when* (server-clock epoch). Bandwidth ≈ zero, so device count is effectively unlimited. Latecomers join mid-track, already in phase. |
| 🌈 **SOLUNA mode** | Phones become a synced light show. Colors are never streamed — each device computes its color from *(zone position, synced clock)*: waves and plasma sweep the crowd, `audio` mode pulses to a client-side RMS envelope of the track. Strobe is capped at 3 Hz for photosensitivity. |
| 📍 **Auto-zoning by GPS** | Register the stage location once from the FOH console; every phone measures its own distance and snaps to the right delay zone as it moves — with hysteresis, accuracy gating, and manual override. |
| 🎧 **DJ from anything** | Open `/dj` on any phone or laptop: mic, line-in (music-grade, echo cancellation off) or a local file becomes the venue-wide source. |
| 🎚 **House-PA fusion** | Feed the FOH desk's matrix out into `source.py --input --lead 0.08` (80 ms measured pipeline), then kill the flam with the console's ±1 ms ALIGN trim, broadcast live to every device. Or run fully standalone. |
| 🔉 **Speaker-node fleet** | `play.py --zone D` turns a Raspberry Pi + class-D amp (~$180/node) into a grid speaker with ±1–3 ms sync over wired/dedicated Wi-Fi. |
| 📲 **PWA** | Installable on Android/iOS home screens, offline shell, ja/en, dark-stage design. |

## Three faces

- **Audience** — `/?zone=B`: pick the flag near you (or let 📍 GPS pick it), tap ▶.
  Everything else is hidden behind "details".
- **DJ** — `/dj`: ON AIR in two taps, level meter, listener count, PA-fusion toggle.
- **Engineer** — `/admin`: zone-distance editor (meters in, delays broadcast live),
  ±ms phase alignment, cue firing with asset picker, light-show VJ panel, per-zone
  device counts. Polling never clobbers your input.

## Festival architecture

```
STAGE (house PA + subs)          ← low end doesn't localize; leave it big
  │ matrix out → source.py --input --lead 0.08
  ▼
SYNC SERVER (any laptop, LAN)    ← ping/pong clock sync, min-RTT window
  ├── speaker grid: 30× Pi nodes, 15 m pitch, wavefront-aligned delays
  └── audience phones: cue + light, zero bandwidth, unlimited count
```

One `shared-cpu` VM or an old MacBook runs the whole thing. On site, run the
server on the FOH laptop (LAN RTT = sub-ms sync); the cloud instance is for
demos and remote rehearsal.

## API

| Endpoint | Description |
|---|---|
| `GET /` `/dj` `/admin` | audience player · DJ broadcaster · FOH console |
| `GET /status` | listeners, zones, active cue/light, stage geo |
| `WS /audio?role=listen&ch=&zone=` | listener (JSON control + SL2 binary) |
| `WS /audio?role=push&ch=` | source (hello `{map,sr,lead}` + SL2 PCM frames) |
| `POST /api/cue` | `{url, lead\|at, gain, loop}` / `{stop}` — synced playback |
| `POST /api/light` | `{pattern, colors, bpm, speed, brightness}` / `{stop}` |
| `POST /api/zones` | `{zones_m:{A:0,B:15,…}}` measured meters → live delays |
| `POST /api/align` | `{base_ms}` global trim vs house PA |
| `POST /api/geo` | `{lat,lng}` stage location for GPS auto-zoning |
| `GET /api/assets` | list pre-distributed tracks |

Admin endpoints require `x-soluna-admin` (env `SOLUNA_ADMIN`). Wire format:
22-byte SL2 header (`"SL2", ver, nchan, pad, seq, nsamp, playAt f64`) + int16
PCM @48 kHz.

## Verified

Protocol suite **36/36 PASS** (clock sync 0.2 ms, identical cue epochs across
devices, mid-track join, live zone/align/geo re-broadcast, 80 ms fusion lead,
auth). Browser E2E on the production deploy: join → sync → light fire → GPS
auto-zone snap, all measured headless. What no CI can verify: how it *feels* in
a real field — bring two phones and find out.

## Roadmap

- **Native iOS** — built as the "Fest" mode of the Koe app (background audio:
  keeps playing locked-in-pocket; sample-accurate `AVAudioTime` scheduling;
  `koe://fest?…` zone-flag QR deep links). Shipping via TestFlight.
- **Native Android** — the PWA already covers Android; a Play Store build follows demand.
- **Acoustic auto-calibration** — chirp from stage, phones measure true acoustic
  distance: delay alignment without GPS or tape measures.
- **Crowd imaging** — per-device pixel coordinates so 5,000 screens can carry
  waves *and* words.

---

## 日本語

**会場が、スピーカーになる。観客が、光になる。**

QRを開いて▶を押すだけで、会場中のスマホ・スピーカーの音と光がミリ秒単位で揃う。
アプリ不要・iPhone/Android/PC対応。音源は事前配布しサーバは「いつ鳴らすか」だけを
配るので、5000台でも帯域はほぼゼロ。色も同じ思想で「パターン+基準時刻」だけを配り、
各端末が自分の位置から色を計算する — だから光の波が観客席を横切る。

- **60秒デモ**: `pip install aiohttp && python3 demo.py` → 出てきたURLをスマホ2台で開く
- **📍GPS自動ゾーン**: FOHがステージ位置を登録すると、各端末が距離を測って自分の
  遅延ゾーンに自動スナップ(精度ゲート+ヒステリシス付き、手動優先可)
- **DJはどの端末からでも**: `/dj` を開いた端末が会場全体の音源になる
- **既存PAと融合も単体運用も**: 実測80msの低遅延パイプライン+±1msトリム
- 本番: フェスではサーバを会場のMac(LAN)で運用。クラウド版はデモ・配布用
- 検証: プロトコル36/36 PASS+本番ブラウザE2E実測。実音場の「耳」での確認はぜひ実機で
