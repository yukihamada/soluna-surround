#!/usr/bin/env python3
"""
SOLUNA Surround source — pushes a 3-channel (L / R / C) PCM stream in real time.

Modes:
  --test            generate a demo bed: soft pad + a blip that rotates L->C->R->C
                    so you can HEAR the sound move across the 3 devices.
  --file song.mp3   decode via ffmpeg to 48k stereo, derive 3 positional channels
                    (L=left, R=right, C=mid). Add --rotate to overlay a moving blip.

Usage:
  python3 source.py --test  --ch festival --server ws://192.168.0.194:8900
  python3 source.py --file ~/Music/x.mp3 --ch festival
"""
import argparse
import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import numpy as np
import websockets

SR = 48000
FRAME = 960                      # 20ms per channel
HEADER = struct.Struct("<3sBBBIId")
POSMAP = ["L", "R", "C"]


def pack_frame(seq, block_i16):
    """block_i16: shape (FRAME, 3) int16 -> SL2 frame (interleaved)."""
    nsamp = block_i16.shape[0]
    hdr = HEADER.pack(b"SL2", 2, 3, 0, seq & 0xFFFFFFFF, nsamp, 0.0)
    return hdr + block_i16.reshape(-1).astype("<i2").tobytes()


def equal_power(angle):
    """angle in [0,3): which of L(0)/C(2)/R(1) ... return gains [gL,gR,gC]."""
    # place positions on a line: L at 0, C at 1, R at 2; pan a point p in [0,2]
    p = angle % 2.0
    # gains via cos^2 between neighboring anchors
    anchors = {0.0: "L", 1.0: "C", 2.0: "R"}
    lo = math.floor(p)
    frac = p - lo
    g = {"L": 0.0, "C": 0.0, "R": 0.0}
    a0 = anchors[float(lo)]
    a1 = anchors[float(lo + 1)] if (lo + 1) in (0, 1, 2) else a0
    g[a0] += math.cos(frac * math.pi / 2) ** 2
    g[a1] += math.sin(frac * math.pi / 2) ** 2
    return g["L"], g["R"], g["C"]


def gen_test_block(t0, n):
    """Return (n,3) float32 in [-1,1]. Rotating blip + soft bed."""
    t = (t0 + np.arange(n)) / SR
    # soft stereo bed (two detuned sines, low level)
    bed = 0.06 * (np.sin(2 * np.pi * 196 * t) + np.sin(2 * np.pi * 294.5 * t))
    out = np.zeros((n, 3), dtype=np.float32)
    out[:, 0] = bed
    out[:, 1] = bed
    out[:, 2] = bed * 0.7
    # blip: 0.25s tone every 0.75s, panned by a slowly rotating angle
    period = 0.75
    for i in range(n):
        tt = t[i]
        ph = tt % period
        if ph < 0.25:
            env = math.sin(ph / 0.25 * math.pi)        # 0..1..0
            angle = (tt / period) % 2.0                 # walks L->C->R->C...
            gL, gR, gC = equal_power(angle)
            blip = 0.5 * env * math.sin(2 * math.pi * 880 * tt)
            out[i, 0] += gL * blip
            out[i, 1] += gR * blip
            out[i, 2] += gC * blip
    return np.clip(out, -1, 1)


def ffmpeg_stereo(path):
    """Decode any file to 48k stereo float32, shape (N,2)."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-ac", "2", "-ar", str(SR),
           "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype="<f4")
    return a.reshape(-1, 2)


async def run(args):
    url = f"{args.server}/audio?role=push&ch={args.ch}"
    tok = os.environ.get("SOLUNA_TOKEN")
    if tok:                       # koe* チャンネルのトークンゲート対応
        url += f"&token={tok}"
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"t": "hello", "map": POSMAP, "sr": SR}))
        safe = url.split("&token=")[0]      # トークンをログに出さない
        print(f"[source] connected {safe} map={POSMAP}")

        seq = 0
        loop = asyncio.get_event_loop()
        start = loop.time()

        if args.file:
            st = ffmpeg_stereo(args.file)
            L, R = st[:, 0], st[:, 1]
            C = 0.5 * (L + R)
            total = st.shape[0]
            print(f"[source] file {args.file}: {total/SR:.1f}s stereo -> L/R/C")
            pos = 0
            while pos < total:
                n = min(FRAME, total - pos)
                block = np.zeros((n, 3), dtype=np.float32)
                block[:, 0] = L[pos:pos + n]
                block[:, 1] = R[pos:pos + n]
                block[:, 2] = C[pos:pos + n]
                if args.rotate:
                    block = np.clip(block + gen_test_block(pos, n) * 0.0
                                    + _rot_overlay(pos, n), -1, 1)
                i16 = (block * 32767).astype(np.int16)
                await ws.send(pack_frame(seq, i16))
                seq += 1
                pos += n
                target = start + pos / SR
                await asyncio.sleep(max(0, target - loop.time()))
        else:
            print("[source] --test rotating blip (L->C->R->C). Ctrl-C to stop.")
            sample = 0
            while True:
                block = gen_test_block(sample, FRAME)
                i16 = (block * 32767).astype(np.int16)
                await ws.send(pack_frame(seq, i16))
                seq += 1
                sample += FRAME
                target = start + sample / SR
                await asyncio.sleep(max(0, target - loop.time()))


def _rot_overlay(t0, n):
    out = np.zeros((n, 3), dtype=np.float32)
    for i in range(n):
        tt = (t0 + i) / SR
        ph = tt % 0.75
        if ph < 0.2:
            env = math.sin(ph / 0.2 * math.pi)
            gL, gR, gC = equal_power((tt / 0.75) % 2.0)
            blip = 0.4 * env * math.sin(2 * math.pi * 1320 * tt)
            out[i] = (gL * blip, gR * blip, gC * blip)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="ws://192.168.0.194:8900")
    ap.add_argument("--ch", default="festival")
    ap.add_argument("--file")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("\n[source] stopped")
