# Running a festival with SOLUNA alone (単体でフェスをやる)

No PA company, no lighting desk, no laptop required. The system is: **Pi boxes as the speakers**,
**a DJ input on one box**, **the phones in the crowd**, **a setlist that runs itself**.

## Kit (for one stage, ~300 people, 30 × 30 m)

| Piece | Count | Notes |
|---|---|---|
| SOLUNA box = Pi 4 + I2S DAC or USB DAC + amp + speaker | 6–9 | 15 m grid over the audience. One box becomes the clock automatically; all are speakers |
| Ethernet + PoE switch (recommended) or the boxes' own Wi-Fi | 1 | Wired boxes elect the server and hold ±0.5 ms; Wi-Fi between boxes works for a small field |
| DJ input | 1 | USB audio interface (or the DJ mixer's USB) plugged into any box → `/setup` → ライブ入力 ON. Or a phone/laptop on `/dj` |
| Power (mains or battery) per box | n | The boxes reboot into the show by themselves after a power cut |
| Printed flags + entrance QR | from `/flags`, `/flags?gate=1` | A4 |
| Phones in the crowd | any | Optional layer for light, effects and sound; LTE or the venue Wi-Fi; a box's own AP takes ~25 |

No internet needed. No account. Everything runs on the boxes.

## Day-of, in order

1. **Power on the boxes.** Wait ~1 min. One of them is the server; open `http://<any box>.local:8900/admin` (or join Wi-Fi *SOLUNA* → the page opens).
2. **Assign zones** in NODES (A near the stage … F at the back) — or leave GPS auto for phones and just name the boxes.
3. **Upload the set** (mp3/aac) in CUE; build the **SHOW** setlist; put a light look on each step.
   Turn **AUTO** on — the runner fires the next track when the current one ends.
4. **Sound check**: 🔊 walk-test each zone from ZONES; fix a box's gain in NODES; ALIGN ±ms by ear if two boxes flam.
5. **Doors**: print the entrance QR; PRELOAD the first track 30 min before.
6. **Show**: press **NEXT** once. Auto-advance runs the night. **MUTE** is the kill switch. A DJ set = `/setup` → ライブ入力 ON on the box the mixer is plugged into (LIVE overrides cues while the source is up).
7. **After**: `/api/stats` → peak devices, cues fired, uptime.

## What "standalone" honestly means

- Sound quality is the boxes' amps and speakers — a 15 m grid of small speakers is *clarity everywhere*, not a 20 kW subwoofer wall.
- Phones are an effects/light layer; the boxes carry the music.
- The boxes' own Wi-Fi holds ~25 devices — enough for crew and DJ, not for the crowd. Crowd phones use LTE + a cloud deploy or venue Wi-Fi.
- If the server box dies, another takes over in 15–20 s (cues keep their epochs); a LIVE DJ stream needs the source to reconnect (the `soluna-source` service restarts by itself).
