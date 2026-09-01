# SOLUNA Surround

**Turn a whole festival into one phase-aligned speaker.**
Clock-synced distributed audio for crowds — cheap speaker nodes on a grid, plus every
phone in the audience, all playing in sync with zone-based delay alignment. Works
standalone or blended into an existing house PA.

*(日本語は下にあります → [日本語](#日本語))*

## Why

Sound only travels at 343 m/s. A big stage PA means people up front get blasted while
people in the back get mud. Pros solve this with **delay towers**; SOLUNA Surround
takes that idea to its logical extreme: put many small speakers *inside* the crowd,
delay each one by `distance/343 + 15ms` (Haas offset), and the image still comes from
the stage — but everyone hears a nearby speaker at a comfortable level.

Two transports, one clock:

| Mode | Use | Scale | Bandwidth |
|---|---|---|---|
| **CUE** | Audience phones playing a pre-distributed file, scheduled to the server clock | unlimited | ~zero |
| **LIVE** (SL2) | Speaker-node fleet / real-time DJ & mic feed over LAN | ~100 nodes | 768 kbps/node |

CUE-mode latecomers join mid-track, already in phase. LIVE mode runs down to
80 ms pipeline lead on wired LAN — low enough to blend with a house PA.

## Quick start

```bash
pip install aiohttp websockets numpy sounddevice
SOLUNA_ADMIN=<secret> PORT=8900 python3 server.py
```

- Audience player: `http://<host>:8900/?zone=B` (put zone QR codes on flags)
- FOH console: `http://<host>:8900/admin` — zones editor, PA alignment trim, cue firing
- Drop audio files in `assets/` → served at `/assets/<file>`

### Fire a cue on every device

```bash
curl -X POST "http://<host>:8900/api/cue?ch=festival" \
  -H "x-soluna-admin: $SOLUNA_ADMIN" \
  -d '{"url":"/assets/opening.mp3","lead":5,"gain":1.0}'
```

### Speaker nodes (Raspberry Pi etc.)

```bash
python3 play.py C --zone D --server ws://<host>:8900   # native client, no browser
python3 source.py --file set.mp3                        # push a file
python3 source.py --input --lead 0.08                   # LIVE: capture FOH matrix out
```

### Blend with an existing house PA

1. Feed a matrix/aux out from the FOH desk into the source machine (`--input --lead 0.08`).
2. House PA covers the front; the grid covers mid/back zones.
3. Play a click through both, then trim `ALIGN` (±1/±5 ms in `/admin`) until the flam disappears.
   The trim broadcasts to every device instantly (`POST /api/align {"base_ms": -12}`).

### Zone math

Enter measured stage distances in `/admin` (or `POST /api/zones {"zones_m":{"A":0,"B":15}}`) —
the server computes `delay_ms = d/343*1000 + 15` and pushes it to all connected devices live.

## How sync works

Every listener runs a ping/pong clock sync against the server (lowest-RTT sample over a
sliding window wins). LIVE frames carry `playAt` (server-epoch time for sample 0) driven
by a sample counter, so network jitter never shifts phase — a frame that arrives too late
is dropped, never played out of phase. CUE mode schedules `AudioBufferSource.start()` at
the exact mapped clock instant; accuracy is limited only by clock sync (~1–3 ms on LAN,
~10–20 ms over LTE).

## API

| Endpoint | Description |
|---|---|
| `GET /` | audience player (`?zone=`, `?d=<meters>`, `?ch=`) |
| `GET /admin` | FOH console |
| `GET /status` | listeners, zones, active cue, stream position |
| `WS /audio?role=listen&ch=&zone=` | listener (JSON control + SL2 binary) |
| `WS /audio?role=push&ch=` | source (hello `{map,sr,lead}` + SL2 frames) |
| `POST /api/cue?ch=` | `{url, lead|at, gain, loop}` or `{stop:true}` — admin |
| `POST /api/zones?ch=` | `{zones_m:{A:0,...}}` or `{zones:{A:15.0,...}}` — admin |
| `POST /api/align?ch=` | `{base_ms: ±ms}` global trim vs house PA — admin |
| `GET /api/assets?ch=` | list files in `assets/` — admin |

Admin endpoints require the `x-soluna-admin` header (`SOLUNA_ADMIN` env).

## Notes & limits

- iOS: audio requires a user tap and the screen to stay on (client uses wakeLock + a
  silent keepalive loop; still, phone layer is best for featured moments, not the main PA).
- The SL2 wire format is 22-byte header (`magic "SL2", ver, nchan, pad, seq, nsamp, playAt f64`)
  + interleaved int16 PCM @48 kHz.
- One server instance comfortably holds thousands of idle CUE listeners; LIVE fanout is
  per-listener, so keep the node fleet on wired/dedicated 5 GHz.

## License

MIT © 2026 Yuki Hamada

---

## 日本語

**フェス会場全体を、位相の揃った1つのスピーカーにする。**

大型PAで遠くまで飛ばす代わりに、客席内に小型スピーカーを格子状に置き、各ノードを
`距離/343秒 + 15ms`(ハース効果)だけ遅らせてクロック同期再生する。音像はステージのまま、
誰の耳にも「近くの小さな音量のきれいな音」で届く。来場者のスマホも CUE モード
(音源事前配布+サーバ時刻一斉スケジュール)でそのまま音場に参加でき、途中参加でも
曲の途中位置から自動同期する。

- 起動: `SOLUNA_ADMIN=<secret> python3 server.py` → 来場者 `/?zone=B`・運営 `/admin`
- 既存ハウスPAとの融合: FOHのmatrix outを `source.py --input --lead 0.08` で取り込み、
  `/admin` の ALIGN(±1/±5ms)でフラムが消えるまでトリム
- ゾーン距離は設営日に実測して `/admin` から一括投入(全端末に即時反映)

検証: ローカル実測 28/28 PASS(クロック同期誤差 0.2ms・キュー開始時刻一致・途中参加同期・
低遅延lead 80ms 等)。実音場の位相確認は各自の環境で(スマホ3台あれば5分で試せます)。
