# Field test: does the crowd's own network carry SOLUNA? (現地回線テスト手順)

**Goal / 目的** — decide, with numbers, whether the audience can ride their own LTE/5G
(plus optional venue Wi-Fi for the front zones) instead of a purpose-built Wi-Fi.
SOLUNA's per-phone traffic is tiny (one ping every 3–10 s, media pre-distributed), so the
question is *latency stability and connection survival*, not bandwidth.

## What you need / 用意するもの
- 3 phones on **3 different carriers** (borrow if needed). Screens unlocked, battery > 50 %.
- The show URL with the **gate QR** (`/flags?gate=1`) and one zone flag QR.
- A notebook or this table. Ideally test at the time of day of the real show.

## Procedure / 手順 (≈ 25 min)
1. **At the entrance** — scan the gate QR on all 3 phones. Time until it says
   *準備OK / Ready*. Record seconds. (Media prefetch → nothing to download later.)
2. **Front (0–20 m from stage)** — tap ▶. Open *詳細/details*. After 30 s read
   **sync (±ms)** and **RTT**. Watch for 5 min: count *disconnects* (status flips to
   offline) and *dropouts* you hear when FOH fires a cue.
3. **Middle (40–60 m)** — same, 5 min.
4. **Back (100 m+)** — same, 5 min.
5. **Stress** — while at the back, have FOH fire a cue with `lead: 3`. Did all 3 phones
   start together? (Ear test: stand them 30 cm apart.)
6. FOH: open `/admin` → DEVICES. Do `playing` / `sync accuracy` match what you see?

## Record / 記録

| Position | Carrier | Gate prep (s) | sync ±ms | RTT ms | Disconnects / 5 min | Cue started together? |
|---|---|---|---|---|---|---|
| Front | | | | | | |
| Middle | | | | | | |
| Back | | | | | | |

## Pass criteria / 合格ライン
- Gate prep **< 30 s** on every carrier (else: put the gate QR earlier in the queue, or
  serve media from the CDN `SOLUNA_ASSET_BASE`).
- sync **±20 ms or better** at all positions → phones are good for effects/light/video.
  ±5 ms or better → phones may carry musical content in that zone.
- **0 disconnects** in 5 min per phone. One disconnect = re-test that spot at peak hour.
- Cue starts together on all 3 (any audible flam → check FINE TRIM, then RTT spread).

## If it fails / 落ちたら
- One carrier bad, others fine → tell the crowd nothing; the sync filter uses the
  lowest-RTT samples and the rest of the field is unaffected.
- All bad at the front only → that's where the venue Wi-Fi goes (announce via
  `POST /api/net {"ssid":…,"wifi_zones":["A","B"]}`; the phones show the hint themselves).
- All bad everywhere → the cell is saturated: bring an AP per Pi node (see README
  "How it scales") or a rented high-density Wi-Fi, front zones first.

Measured baselines for reference: LAN ±1–3 ms, LTE typically ±10–20 ms, phone hotspot
under a 2,000-client burst 1.7 s RTT (radio-bound, server at ≤40 % CPU).
