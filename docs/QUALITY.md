# SOLUNA Sound — persona scorecard (7 × 7)

Seven people have to love this for a festival to adopt it. Each gets seven criteria,
each scored 0–100 with the evidence that earns the score. "100" means *done and
verified*; anything only verified in software says so. Re-scored on every release.
Scores below are for **v7** (2026-09-02 night, 261 automated checks green; Pi 4 field run
for election / assign / self-heal / mute done on one box). Owner of each gap in brackets.

Legend: ✅ 100 · 🟡 partial (score shown) · 🔴 missing · 🧪 100 in software, field-pending

## 1. Artist

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | my track to hit every speaker and phone at the same instant | ✅ | CUE mode, sample-accurate WebAudio + Pi nodes ±0.5 ms; ears-on test "perfect" |
| 2 | my setlist to run as I wrote it, one button | ✅ | Show runner `/api/show` NEXT/goto, steps bundle track+video+light |
| 3 | my visual identity per song | ✅ | Light look per step (pattern + 2 colors + bpm), video cue, image cue for interludes |
| 4 | to hear/see what the crowd gets before doors | ✅ | Walk-test cue per zone, `/screen`, join as audience on my own phone |
| 5 | to play live, not just files | ✅ | LIVE SL2 from any interface: analog/USB/Dante Virtual Soundcard/AVB `--input`, AES67/Dante-AES67 `--aes67` |
| 6 | my name and title on every phone | ✅ | cue `title`/`artist` → NOW PLAYING on phones and screen; cleared on stop |
| 7 | to hand over files without getting the master key | 🟡 60 | Upload needs the admin token; per-artist upload tokens [roadmap] |

## 2. FOH sound engineer

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | to feed it from my desk in the format I already run | ✅ | Dante (DVS / AVIO / AES67 mode), AES67/Ravenna (`--aes67`, `--sap` discovery), MADI/AVB/analog via interface (`--input`) — docs/integration.md |
| 2 | a known latency budget | ✅ | 80 ms fusion path measured; d/343+15 ms zones; README table |
| 3 | alignment tools I trust | ✅ | ALIGN stepper (`/api/align`), zones by metres (`zones_m`), per-device FINE TRIM |
| 4 | to see level and know the feed is alive | ✅ | LIVE level meter (peak/RMS dBFS + clip) in `/status` and `/admin`; −6 dBFS square wave measured −6.0 in test |
| 5 | a kill switch | ✅ | `/api/mute`: real Pi node logs `MUTE on/off` and keeps phase; headless phone gain 0.8 → 0 → 0.8 within a second |
| 6 | redundancy that doesn't need me | ✅ | Pi mesh election + warm-standby state snapshot → automatic takeover ≈15–20 s; manual `/api/state` still there |
| 7 | to know how many are actually sounding | ✅ | DEVICES panel: playing/preloaded/failed/ctx-suspended per zone, node table |

## 3. Lighting / video / show-control operator

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | to fire it from my console | ✅ | OSC in (`/soluna/cue`, `/go`, `/light`…) — QLab / grandMA / Eos / Ableton examples in docs/show-control.md |
| 2 | my rig to follow its looks | ✅ | Art-Net + sACN out, 40 Hz, same pattern math as the phones |
| 3 | timecode | ✅ | `/api/timecode` + OSC `/soluna/tc`; cues by `tc`; LTC decoder `ltc.py` (24/25/30/29.97DF round-trip tested) |
| 4 | photosensitivity safety | ✅ | Strobe capped 3 Hz; phones honour OS "reduce motion" (strobe→pulse, plasma→solid) |
| 5 | a preview | ✅ | `/screen` on any laptop, walk-test per zone |
| 6 | frame-accurate video | ✅ | 23 ms drift, 2 ms loop-join measured |
| 7 | it to never take my network down | ✅ | Light = pattern+epoch only (zero bandwidth); OSC/Art-Net off by default in cloud |

## 4. Production / promoter

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | to know what it costs | ✅ | Node BOM + wireless options with honest capacity numbers (internal estimate; README states rules of thumb) |
| 2 | a runbook my crew can follow | ✅ | JA checklist + engineer section + docs/pi-box.md + field-test.md |
| 3 | printed collateral | ✅ | Zone flags `/flags`, entrance QR `/flags?gate=1` (A4, QR) |
| 4 | to not get sued | ✅ | No location upload, no accounts, strobe cap, privacy text ja/en |
| 5 | numbers I can repeat to a sponsor without lying | ✅ | Verified table distinguishes LAN/cloud/field; unverified items named |
| 6 | sponsor moments | ✅ | Image cue on phones/screen between sets, NOW PLAYING branding line |
| 7 | a report afterwards | ✅ | `/api/stats` (admin): peak devices, peak playing per zone, cues fired, uptime |

## 5. Audience

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | one tap, no app | ✅ | PWA, QR → ▶ |
| 2 | it in my language | 🟡 70 | ja/en complete; more locales [roadmap] |
| 3 | my battery to survive the night | ✅ | Auto-dim 45 s, GPS polling relax, ping relax 10 s |
| 4 | my location kept to myself | ✅ | Distance computed on-device; only zone letter sent |
| 5 | the sound to keep going when I lock the phone | 🟡 40 | Web can't (iOS); native Koe integration does — human gate: TestFlight |
| 6 | no flashing that hurts | ✅ | 3 Hz cap + reduce-motion respected |
| 7 | to know what I'm hearing | ✅ | NOW PLAYING title/artist |

## 6. Stage crew / node operator

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | flash, power on, done | 🧪 95 | Zero-config agent on the real Pi 4: booted → elected itself server in 12 s → node repointed to localhost (±0.5 ms). Two-box election/yield only unit-tested [field: 2nd Pi] |
| 2 | no laptop needed | ✅ | Lone Pi = server + node verified; the Wi-Fi AP raised itself for real when the tethering uplink dropped (SSID SOLUNA seen from the Mac). Now with grace / autoconnect-off / retry / known PSK so it never strands itself |
| 2c | join without a password, still safe | ✅ | Open AP + nft/iptables gate (80/8900 only, no SSH, no forwarding), client isolation, `/setup` open only 10 min after power-on, `/admin` always token; owe/wpa selectable |
| 2b | plug in → a page opens, settings without SSH | 🧪 95 | Captive portal (Apple/Android/Windows probes) + `/setup` (role, speaker, Wi-Fi, AP, hostname, token, update, logs, 🔔 tone) — 34 headless checks; real-phone sheet pop-up pending [field] |
| 3 | it to heal itself | ✅ | Real Pi: `kill -9` of the server → back in 6 s (systemd + agent), assigned zone survives, node keeps sync; 20 s start grace so a cold start is never mistaken for a crash; udev DAC hot-plug; hardware watchdog armed (reboot path itself not provoked) |
| 4 | it to survive the server box dying | 🧪 85 | Standby snapshot + takeover implemented and unit-tested; real two-Pi takeover timing pending [field: 2nd Pi] |
| 5 | to assign zones without SSH | ✅ | Real Pi: `/api/nodes/assign` zone C → node log `ASSIGN zone=C` within 3 s, `node.json` written, walk-test cue on C played |
| 6 | to see which box is unhappy | ✅ | NODES: temp, load, disk, audio device, last seen, stale flag |
| 7 | any DAC to just work | ✅ | Auto-pick USB > I2S (HiFiBerry/PCM5102A) > built-in; verified on Pi 4 + PCM5102A |

## 7. Developer / integrator

| # | I want… | Score | Evidence / gap |
|---|---|---|---|
| 1 | open source, permissive | ✅ | MIT |
| 2 | tests and CI that gate deploys | ✅ | protocol/node/auth/aes67/showcontrol/mesh suites run before deploy |
| 3 | an API I can read in 5 minutes | ✅ | README API table, OSC table, wire format SL2 |
| 4 | a container | ✅ | Dockerfile, fly.toml |
| 5 | to write my own node/client | ✅ | play.py is the reference node (~400 lines); client.html zero-dependency |
| 6 | versioned protocol | ✅ | `ver` in config, `/health.version` |
| 7 | to run it anywhere | ✅ | Mac laptop, Pi, cloud — same process |

### Open gaps (what keeps a row under 100)
- Artist #7 per-artist upload tokens [roadmap] · Audience #2 more locales [roadmap] · Audience #5 needs the native app [human gate: TestFlight]
- Stage crew #1/#2/#4 need a second Pi and a Pi with no uplink [human gate: hardware]
- Everything in "Lighting" and "FOH #1" is verified against synthetic packets/audio only — a real console, Dante device or LTC source is the last mile [human gate: venue tech]

Total: 51 rows · 45 at 100 · 6 held below 100 only by hardware or a human decision.
