# SOLUNA Sound

**The venue is the speaker. The crowd is the light show.**

SOLUNA Sound turns every phone, laptop, projector and cheap speaker in a space into
one phase-aligned system for **sound, light and video**. No app installs — one link,
one tap, and you're inside it. One server process verified at **10,000 concurrent
devices on a LAN** (cloud path measured to ~3k from a single test IP).

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

### Running the whole night

The console is not just buttons — it runs a show. Build a **setlist** where each
step bundles a track, a video and a light look; the **NEXT** button fires them as
one synchronized moment. A single zone can be **walk-tested** (`{zones:["B"]}`
targets cue delivery) while the rest of the field stays silent. And the show is
**crash-safe**: zones, alignment, stage geo, the active cue/light and the setlist
position persist to `state.json` and come back on boot. Zone flags print
themselves from `/flags` — A4, big letter, QR straight into the zone.

### Running it for real (operations)

- **Observability** — every device reports what it is *actually* doing
  (`preloaded / playing / idle / failed`, AudioContext state, battery, sync accuracy;
  never its location). `/status` aggregates them and the console shows
  *"how many are really sounding"*, per zone — not just how many are connected.
- **Speaker nodes run the same show** — `play.py` (Raspberry Pi) receives CUE,
  PRELOAD, walk-test, SHOW steps and LIGHT (forwarded to a `--light-cmd` GPIO hook),
  decodes any format via ffmpeg, joins mid-track, reconnects forever.
- **Clock authority is monotonic** — the server timestamps from `monotonic()`
  anchored once at boot, so an NTP step on the host can never jump the crowd.
- **Hot standby** — `GET /api/state` exports the whole show; `POST /api/state` on a
  second machine re-broadcasts it and phones resume mid-track. Both laptops NTP-synced
  → cue epochs stay valid across the switch.
- **Power** — audience screens auto-dim to black after 45 s idle (light show
  excepted), GPS drops from continuous watch to 60 s polls once the distance settles,
  battery level is reported so FOH can see a crowd running low.
- **Level trim** — a per-device ±12 dB slider (phone speakers differ by 6–10 dB
  between models) plus per-zone gain from the console.
- **Asset delivery** — `/assets/` is served with `Cache-Control: public` and CORS;
  set `SOLUNA_ASSET_BASE=https://cdn…` and every device fetches tracks from your
  CDN/R2 **first**, falling back to the sync server if the object isn't there yet
  (PRELOAD burst leaves the VM entirely; an unsynced upload still plays).
  `tools/r2-sync.sh` pushes the assets folder to the bucket (R2: create bucket →
  `wrangler r2 bucket cors set --file tools/r2-cors.json` → custom domain → sync;
  note wrangler v4 needs `--remote` on object puts, the script passes it).
- **Persistent data** — `SOLUNA_DATA_DIR` (a Fly volume at `/data` in the shipped
  config) holds tracks and `state.json` across redeploys.
- **Privacy** — coordinates never leave the phone; only the nearest zone letter is
  sent. The audience page says so, in ja/en.

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
| `POST /api/show` | `{steps:[{label, url?, video?, light?}]}` set the setlist · `{next:true}` fire the next step · `{goto:i}` jump |
| `GET /flags` | print-ready A4 zone flags with QR codes |
| `GET /api/assets` · `GET /status` | asset list · listeners/zones/cue/light/show state + `devices` summary |
| `GET/POST /api/state` | export / import the whole show (hot standby) |
| `DELETE /api/channel?ch=` | drop a test channel's saved state |
| `GET /health` | liveness (version, uptime, listeners) — used by Fly checks and CI smoke |
| `WS → {t:"report",…}` | device → FOH state report (state, ctx, battery, sync accuracy) |

Admin endpoints take `x-soluna-admin` (env `SOLUNA_ADMIN`). Broadcasts are
parallel with per-socket timeouts — one dying phone can't stall the crowd.

## Verified

| Test | Result |
|---|---|
| Protocol suite (`tests/`, runs in CI before every deploy) | 65 protocol + 20 node + 9 auth/load checks PASS (clock, identical cue epochs, mid-join, live re-broadcast, device reports, state export/import, cache headers) |
| **10,000 concurrent devices — one process, LAN** | 9,999/10,000 connected in 17 s, **cue reached 100%**, identical epochs, 791 ms delivery spread vs 8 s lead, median RTT under load 29 ms |
| Video sync | 23 ms drift; 2 ms mid-track loop join |
| Production E2E | join → sync → light → GPS auto-delay, headless browser vs the live deploy |
| **Real devices** | two iPhones, ears-on: music, light show and timecode video locked — then a real track with onset-flash lighting |

Honest limits: the 10k figure is a local (LAN) measurement of the server process; a
10k run against the cloud deploy from a single test IP hits NAT limits at ~3k and
has not been re-run from distributed sources. Real crowds are 10k distinct IPs and
show-day runs the server on the FOH laptop (LAN) anyway. iOS Safari stops web audio
on lock screen (the native Koe integration doesn't); LTE sync (±10–20 ms) is for
effects — wired nodes carry the main PA duty. Not yet done in the field: outdoor GPS
accuracy, a physical Pi node, dozens of real iPhones at once.

## Deploy

```bash
SOLUNA_ADMIN=<secret> SOLUNA_DJ_TOKEN=<secret> PORT=8900 python3 server.py
```

Included `fly.toml` ships `connections` concurrency (20k hard) — the default
few-hundred cap silently rejects a crowd — a `/health` check, and a 1 GB volume at
`/data` (`SOLUNA_DATA_DIR`) so uploaded tracks and `state.json` survive redeploys.
CI runs the test suite first and smokes `/health` after the deploy.

```bash
python3 tests/test_node.py && python3 tests/test_protocol.py && python3 tests/test_djauth_load.py
```

## Roadmap

- **Native iOS** — shipped as the Koe app's "Fest" mode (background audio,
  sample-accurate `AVAudioTime`, zone-flag QR deep links `koe://fest?…`); LED-torch
  sync next. Web/Android enter via `koe.live/fest`.
- **Acoustic auto-calibration** — a stage chirp measured by the mic: true acoustic
  distance, no GPS, no tape measure.
- **Crowd imaging** — per-device pixel coordinates so 10,000 screens can carry
  pictures, not just waves.
- **Node lighting driver** — a reference `--light-cmd` for WS281x strips on the Pi
  nodes (the hook and the pattern protocol exist; the LED driver script does not yet).
- **Acoustic level calibration** — pink-noise self-measurement through the phone mic
  to set the per-device trim automatically (today it is a manual ±12 dB slider).

---

# 日本語ドキュメント

## これは何か(主催者の方へ)

**来場者がスマホでQRを読んで▶を1回押すだけで、会場中の音・光・映像がミリ秒単位で揃う**
システムです。アプリのインストールは不要(iPhone/Android/PCのブラウザで動きます)。

- 客席のどこにいても「近くから、心地よい音量で、明瞭な音」が届く — 前方だけ爆音・後方は
  こもる、という大型PAの宿命を物理から解決します
- 合図ひとつで**全員のスマホ画面が同期したカラーライト**になり、光の波が客席を横切る。
  オープニング映像を1万台の画面+ステージスクリーンで同時に流すこともできます
- **既存の会場音響と共存**できます(メインスピーカーとサブは会場のものをそのまま使い、
  本システムは中後方の明瞭度と演出を担当)。単体でも完結します
- 実証済み: **1台のサーバで1万台同時接続(LAN実測)・合図の到達率100%**・実機iPhoneでの実聴テスト済み

### 主催者チェックリスト

| 必要なもの | 内容 |
|---|---|
| サーバ | ノートPC(Mac)1台。会場のLANに置く(クラウド版はリハ・配布用) |
| 来場者側 | 各自のスマホのみ。ゾーン旗のQR(またはGPS自動)で参加 |
| ゾーン旗 | `/flags` を開いてそのままA4印刷(ゾーン文字+QR入り) |
| 予備機 | ノートPCをもう1台(ホットスタンバイ)。主機の `/admin` → STATE → EXPORT で控えを取り、障害時は予備で IMPORT |
| 電池 | 来場者の画面は放置45秒で自動暗転・GPSは距離確定後60秒間隔に落として節電。FOHで電池20%未満の台数が見える |
| 位置情報 | 緯度経度は端末外へ出ません(最寄りゾーン記号のみ送信)。来場者ページに日英で明記済み |
| 会場との調整 | FOH卓のmatrix/aux出力を1系統(既存PAと融合する場合)・電源・持込機器の申請 |
| オプション | 客席内スピーカーノード(Raspberry Pi+アンプ、1台約$180)・スクリーン用Mac |
| 安全 | ストロボ演出は光過敏対策で3Hz上限を実装済み。音量は各端末で調整可能 |
| 副次効果 | 分散小音量なので**敷地外への音漏れが構造的に小さい** — 近隣・行政協議の材料になります |

機材構成・費用感・当日ランブックの詳細は設計書(SOLUNA Sound Grid)を参照してください。

## 音響エンジニアの方へ — 原理と運用

### 原理: ディレイタワーの細粒度化

音速は343m/s。従来のディレイタワーと同じ発想で、客席内の**すべての再生点(スマホ/ノード)に
「ステージからの距離 ÷ 343 + 15ms」のディレイ**をかけ、ステージ音の波面に正確に乗せます。
+15msのハース効果ぶんで定位はステージに残り、音圧と明瞭度は目の前の再生点が供給します。
低域は定位しないため、サブは従来どおりステージ集中(会場PAのものを使用)で問題ありません。
帯域の住み分けは「グリッド=おおよそ200Hz以上/会場PA=フルレンジ+低域」が自然に成立します
(ノード側が小口径のため)。

### 信号フロー

```
FOH卓 matrix/aux out ──▶ USBオーディオIF ──▶ source.py --input --lead 0.08
                                                    │ (SL2/48kHz/16bit,
                                                    ▼  srは自動追従)
                                              同期サーバ(Mac)
                                                    │ WebSocket
        ┌──────────────┬────────────────┬───────────┴──────┐
        ▼              ▼                ▼                  ▼
   来場者スマホ    Piスピーカーノード   /screen(映像)     Koe iOSアプリ
   (LTE/会場WiFi)  (有線/専用5GHz)     (Mac+HDMI)        (ロック中も再生)
```

### 同期の仕組みと精度

- 各端末はサーバとping/pongで時刻合わせ(片道=RTT/2と仮定し、**直近30サンプルのうち
  最小RTT側3点の中央値**を採用 — 経路非対称に強い)。収束後は3秒間隔に自動減速
- さらに**端末固有の出力レイテンシ**(`AudioContext.outputLatency`、機種間で10〜30ms差)を
  各端末が自己申告で差し引くため、機種混在でも揃います。残差は端末側の±50ms微調整で追い込み可
- 実測精度: 有線/専用AP **±1〜3ms** / LTE **±10〜20ms**(音の0.3〜7m相当)。
  したがって**主音響は有線ノード層、スマホ層は演出**という役割分担が設計意図です

### レイテンシバジェット(LIVE=既存PA融合時)

| 区間 | 値 |
|---|---|
| 取り込み(AudioWorklet)+SL2化 | 〜10ms |
| LAN往復+サーバ | 〜5ms |
| サーバ先読み(`--lead`) | **80ms(実測・可変)** |
| 合計パイプライン | **約95ms** |

パイプライン合計が「グリッド最前列の物理ディレイ(例: 30m=87ms)」を下回っていれば、
各端末が不足分を足して波面に乗ります。つまり**グリッドの最前列より手前は会場PAの担当**です。

### 当日ワークフロー

1. **設営**: サーバMacを会場LANへ。`/admin` を開く
2. **距離入力**: 各ゾーンのステージ距離をメジャー実測して ZONES に入力(全端末へ即時配信)。
   またはステージ前で「📍現在地をステージに設定」→ 来場者はGPSで自動ディレイ
3. **PA位相合わせ**: クリック/リムショットを会場PAとグリッド両方から出し、
   **フラム(二度打ち感)が消えるまで ALIGN を±5→±1msで追い込む**(全端末一括・即時反映)
4. **ウォークテスト**: ZONES表の🔊でそのゾーンだけに音を出し、歩いて確認。
   **音量差**は ZONES の gain(dB) で遠いゾーンを持ち上げる。機種差は来場者側の±12dBスライダー
5. **PRELOAD**: 開演30分前までに音源・映像を📥PRELOAD(全端末が事前DL — FIRE時の
   ダウンロード集中をゼロにする)。DEVICESパネルの **preloaded** が来場者数に近づくのを見る。
   大規模ならCDN/R2を `SOLUNA_ASSET_BASE` に指定して配布を同期サーバから外す
   (端末はCDN→サーバ直の順に取りに行くので、CDN未同期の曲でも止まらない)。
   アップロード後は `tools/r2-sync.sh` でバケットへ同期してからPRELOAD
6. **本番**: SHOWパネルにセットリスト(各ステップ=曲+映像+ライトの束)を組み、
   **NEXTボタンだけで進行**。単発はFIRE/LIGHTで割り込み可。DJ交代は `/dj` の
   トークン付き招待リンクを渡すだけ
7. **強制終了**(ハードカット): STOP 1操作で全端末即時無音・消灯
8. **障害時**: サーバ再起動でゾーン・位相・セットリスト・進行位置は自動復元
   (state.json)。電源が飛んでもサウンドチェックは消えない。**主機が死んだら**予備Macで
   STATE → IMPORT(控えは開演前に EXPORT)。端末は再接続後に進行中の曲へ曲中復帰する
   (両Macの時計をNTPで揃えておくこと)
9. **本番中の監視**: DEVICESパネル — **playing**(実際に鳴っている台数)/ **failed** /
   **ctx suspended**(ロック・サイレントで無音の可能性)/ **battery<20%**。接続数でなく
   「鳴っている数」を見る

### 音質・信頼性の仕様

- 48kHz/16bit PCM(SL2)。配信元のサンプルレート(例: Macの44.1kHz)は全端末が自動追従
- サーバの時計は**monotonic基準**(起動時に一度だけepochを取る) — ホストのNTP補正で
  全端末が一斉に飛ぶことがない
- Piスピーカーノード(`play.py`)はスマホと同じプロトコルを全部話す: CUE/PRELOAD/
  ウォークテスト/SHOW/ライト(GPIOフック)。ffmpegで任意形式をデコード・曲中合流・
  切断は必ず再接続
- ライブ系のフレームは**サンプルカウンタ駆動のplayAt**を持ち、ネットワークジッタで
  タイムラインが曲がらない。間に合わないフレームは「位相を崩して鳴らす」のではなく捨てる
- 全ブロードキャストは並列送信+ソケット毎タイムアウト — 瀕死の1台が全体を止めない
- 切断は自動再接続(音楽は時計から再計算して曲中復帰)。配信(push)はトークン認証で乗っ取り防止
- 実証: **1万台同時(LAN・単一プロセス)**(到達100%・開始時刻完全一致・配信ばらつき791ms/猶予8秒)・
  実機iPhone2台の実聴で音/光/映像の同期を確認済み。テスト94項目はCIで毎デプロイ前に実行

### 既知の制約(正直に)

- iOSのWeb版は画面ロックで音が止まる(→ネイティブアプリ版は継続再生。Web版は
  「画面はつけたまま」の案内文言+放置時の自動暗転(ロック不要で節電)を実装済み)
- 未実施の物理検証: 屋外GPS実精度・Piノード実機・実iPhone多台数。1万台のクラウド経路
  負荷試験は単一IPから約3千で頭打ち(NAT)のため未完 — 本番は会場LAN運用が正
- LTE経由の±10〜20msはタイトな主音響には粗い — 主音響は有線ノード、スマホは演出に
- クラウド版(fly.io)は遠隔リハ・配布用。本番はFOHのMacでローカル運用が正
