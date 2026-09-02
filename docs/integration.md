# Plugging SOLUNA into an existing festival

SOLUNA does not replace the house system. It takes **one feed** from the FOH desk — the
same matrix/aux you would send to a delay tower — and puts it on every phone and Pi node
in phase with the stage. This page is about getting that feed in, whatever the venue's
audio network speaks.

## Formats → how

| What the venue has | How SOLUNA takes it | Command |
|---|---|---|
| **Dante** device with **AES67 mode** enabled (most Dante hardware since 2016: Yamaha CL/QL/DM7/Rivage, Allen & Heath dLive/SQ, DiGiCo, Focusrite RedNet, Audinate AVIO…) | In Dante Controller: enable AES67 on the transmitter, create an **AES67 multicast flow** for the channels you want (e.g. Matrix 7/8). SOLUNA joins that multicast group — no Dante license, no Dante software. | `python3 source.py --sap` then `python3 source.py --aes67 auto --lead 0.08` (or `--aes67 239.69.x.x:5004`) |
| **AES67 / Ravenna** (Lawo, Merging, Genelec, Neumann, most broadcast gear) | Same: subscribe to the announced multicast stream. SAP discovery lists names; pick by name substring. | `--aes67 "Stagebox"` |
| **Dante without AES67 mode** (older firmware, Ultimo chips) | **Dante Virtual Soundcard** on the FOH Mac that runs the sync server → DVS becomes an audio input device → `--input`. Or a **Dante AVIO USB** adapter into the Mac/Pi. | `--input "Dante Virtual Soundcard" --lead 0.08` |
| **MADI**, **AVB/Milan**, **analog**, **AES3**, **USB** | Any interface macOS/Linux sees as an input device. macOS is an AVB entity natively (Thunderbolt/USB-C Ethernet). | `--input [device]` — list with `python3 -c "import sounddevice;print(sounddevice.query_devices())"` |
| **Files** (pre-recorded show, playback-only stage) | Decoded via ffmpeg, any format; or pre-distribute as a CUE (no streaming at all). | `--file show.wav` · `POST /api/cue` |

Channel mapping for every LIVE mode: `--map "L=1,R=2,C=1+2"` (default). `+` averages
channels, so `C=1+2` is the mono sum of the pair and `--map "L=3,R=4,C=3+4"` takes
channels 3/4 of an 8-channel stream.

## AES67 receive details

- RTP/UDP multicast (or unicast to the host), payload **L24 or L16**, 48 kHz, 1 ms (48
  samples) or 125 µs (6 samples) packets, any channel count; format/channel count come
  from the SDP announced over **SAP** (`239.255.255.255:9875`) or from `--aes67-fmt` /
  `--aes67-ch`.
- **Lost or reordered packets** are handled by RTP timestamp, not arrival: a gap is filled
  with exactly that many zero samples, late/duplicate packets are dropped, so the sample
  count — and therefore the phase of every downstream device — never slips. A hole larger
  than 0.5 s is treated as a restarted stream.
- **No PTP required.** SOLUNA's timeline is the sync server's own clock: it stamps
  `playAt` from the running sample count of the feed it receives. The sender's PTP domain
  is irrelevant to the audience; the feed just has to be a continuous sample stream.
- Multiple NICs: `--iface 192.168.10.5` selects the interface that joins the group.
- Verified with a synthetic AES67 sender (L24, 2 ch, 1 ms; 10 % packet loss) end to end
  through the server to a listener: level, spectrum and sample count exact
  (`tests/test_aes67.py`). **Not yet verified against real Dante/Ravenna hardware.**

## Latency budget (fusion with the house PA)

Use `--lead 0.08` so the phone/node layer sits ~80 ms behind the desk, then align the
**whole field** to the house PA once with `POST /api/align` (a ±ms trim on every device)
until a click's flam disappears. The per-device `distance/343 + 15 ms` delay is added on
top, per position — exactly like a delay tower. Measured pipeline (README, "レイテンシ
バジェット"): capture + SL2 ≈ 10 ms, network + lead 80 ms, device output latency
10–30 ms (self-reported and subtracted). The AES67 path removes the USB interface hop
(≈ 5–10 ms less jitter than `--input`), everything else is identical.

## Honest limits

- **Receive only.** SOLUNA does not transmit AES67/Dante; it is a sink, like a delay
  tower amp.
- **No native Dante protocol.** Dante's proprietary transport is licensed; we use the
  standardised AES67 mode Dante devices expose, or DVS/AVIO. If the desk cannot enable
  AES67, use `--input`.
- **No PTP slave.** Not needed for SOLUNA's own sync (above), but it also means SOLUNA
  cannot be *the* PTP-locked device in someone else's AES67 chain — it never has to be.
- **48 kHz only** for AES67 (no resampling in the receiver; 96 kHz flows are refused with a
  warning). The house side stays 48 kHz for a live PA anyway.
- **Unicast to the sync host works** (tested), multicast needs IGMP to be allowed on the
  venue switch VLAN you plug into — ask the network tech for the Dante/AES67 VLAN and an
  access port; that is all.

---

## 既存フェスへの組み込み(日本語・要点)

SOLUNAは会場PAを置き換えません。FOH卓から**ディレイタワーに送るのと同じ1フィード**を受け、
客席内の全スマホ/ノードにステージと同相で出します。受け口は3つ:

1. **Dante(AES67モード)/ AES67 / Ravenna** — Dante Controllerで送信側のAES67を有効化→
   欲しいch(例 Matrix 7/8)の**AES67マルチキャストフロー**を作成。同期サーバのMac/Piを
   音響ネットワークの空きポートに1本挿し、`python3 source.py --sap` で一覧 →
   `python3 source.py --aes67 auto --lead 0.08`。Danteライセンス・Danteソフト不要。
   パケット欠落はタイムスタンプで無音埋め=位相は絶対にずれない。PTP不要(時刻の親は同期サーバ)。
2. **DanteでAES67モードが使えない卓** — FOH MacにDante Virtual Soundcard、または
   Dante AVIO USBアダプタ → `--input "Dante Virtual Soundcard" --lead 0.08`。
3. **MADI / AVB / アナログ / AES3 / USB** — OSが入力デバイスとして見えるものは全部 `--input`。

ch割り当ては全モード共通 `--map "L=3,R=4,C=3+4"`(`+`=平均)。

**制約(正直に)**: 受信のみ(送出しない)・ネイティブDanteプロトコル非対応(ライセンス品のため
AES67モード/DVS/AVIO経由)・PTPスレーブではない(SOLUNAの同期には不要)・AES67は48kHz固定・
マルチキャストは会場スイッチのDante/AES67 VLANでIGMPが通るポートをもらう(ユニキャスト直送も可)。
**実機Dante/Ravenna機材での検証は未実施**(合成AES67送信で level/スペクトル/サンプル数一致まで
`tests/test_aes67.py` で確認済み)。
