#!/usr/bin/env python3
"""Verify SOLUNA Surround: clock-sync convergence, positional channel
separation, and cross-device playAt agreement (the core of phase-aligned
surround). Connects two listeners (L and R) and replicates the client math."""
import asyncio, json, struct, time, sys
import websockets

HEADER = struct.Struct("<3sBBBIId")
SR = 48000
SERVER = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8900"


async def listen(pos, n=120):
    url = f"{SERVER}/audio?role=listen&ch=festival&pos={pos}"
    seq_play = {}        # seq -> playAt
    first_samples = {}   # seq -> first 6 int16 samples
    best_rtt = float("inf"); offset = None
    async with websockets.connect(url, max_size=None) as ws:
        # a few clock-sync round trips
        for _ in range(8):
            c = time.perf_counter() * 1000
            await ws.send(json.dumps({"t": "ping", "c": c}))
            # drain until we get the pong
            while True:
                msg = await ws.recv()
                if isinstance(msg, str):
                    m = json.loads(msg)
                    if m.get("t") == "pong":
                        now = time.perf_counter() * 1000
                        rtt = now - m["c"]
                        if rtt < best_rtt:
                            best_rtt = rtt
                            offset = (m["s"] + rtt / 2) - now
                        break
                else:
                    _record(msg, seq_play, first_samples)
            await asyncio.sleep(0.03)
        # collect frames
        while len(seq_play) < n:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                _record(msg, seq_play, first_samples)
    return {"pos": pos, "rtt": best_rtt, "offset": offset,
            "seq_play": seq_play, "samples": first_samples}


def _record(buf, seq_play, first_samples):
    if len(buf) <= HEADER.size:
        return
    magic, ver, nchan, _p, seq, nsamp, play_at = HEADER.unpack_from(buf, 0)
    seq_play[seq] = play_at
    arr = struct.unpack_from("<6h", buf, HEADER.size)
    first_samples[seq] = arr


async def main():
    L, R = await asyncio.gather(listen("L"), listen("R"))
    print("=== clock sync (localhost, expect ~0) ===")
    for r in (L, R):
        print(f"  pos {r['pos']}: best_rtt={r['rtt']:.3f}ms  offset={r['offset']:.3f}ms")

    shared = sorted(set(L["seq_play"]) & set(R["seq_play"]))
    print(f"\n=== playAt agreement across positions ({len(shared)} shared frames) ===")
    max_diff = 0.0
    for s in shared:
        d = abs(L["seq_play"][s] - R["seq_play"][s])
        max_diff = max(max_diff, d)
    print(f"  max |playAt_L - playAt_R| over shared seqs = {max_diff*1e9:.1f} ns")
    print("  -> identical playAt per seq => every device targets the SAME instant")

    # monotonic step == FRAME/SR
    seqs = sorted(L["seq_play"]); steps = []
    for a, b in zip(seqs, seqs[1:]):
        if b == a + 1:
            steps.append(L["seq_play"][b] - L["seq_play"][a])
    if steps:
        exp = 960 / SR
        import statistics
        print(f"\n=== playAt cadence ===")
        print(f"  step mean={statistics.mean(steps)*1000:.4f}ms  expected={exp*1000:.4f}ms "
              f"(jitter max dev={max(abs(x-exp) for x in steps)*1e9:.1f} ns)")

    print("\n=== positional channel separation (L vs R payload) ===")
    diff_frames = same = 0
    for s in shared:
        if L["samples"][s] != R["samples"][s]:
            diff_frames += 1
        else:
            same += 1
    print(f"  frames where L payload != R payload: {diff_frames}/{len(shared)}  (identical: {same})")
    ex = shared[len(shared)//2]
    print(f"  e.g. seq {ex}:  L={L['samples'][ex][:4]}  R={R['samples'][ex][:4]}")

    ok = (max_diff < 1e-6) and (diff_frames > 0)
    print(f"\nRESULT: {'PASS ✅' if ok else 'FAIL ❌'} — "
          f"playAt identical across devices AND channels positionally distinct")

asyncio.run(main())
