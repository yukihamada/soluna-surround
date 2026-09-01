#!/usr/bin/env python3
"""
SOLUNA Surround — native auto-playing speaker (no browser, no tap).

Connects to the server as a LISTEN device for a position (L/R/C), runs the
same ping/pong clock-sync, and schedules every frame at its server `playAt`
into a shared stream-sample ring buffer so multiple devices stay phase-locked.
Pans its mono channel to its position so even a single stereo Mac demonstrates
the L->C->R movement.

  python3 play.py L --server ws://127.0.0.1:8900 --ch festival
"""
import argparse, asyncio, json, struct, threading, time, sys
import numpy as np
import sounddevice as sd
import websockets

SR = 48000
HEADER = struct.Struct("<3sBBBIId")
RING_SEC = 4
RING = RING_SEC * SR
PAN = {"L": (1.0, 0.0), "C": (0.7071, 0.7071), "R": (0.0, 1.0)}


class Player:
    def __init__(self, pos, zone=None):
        self.pos = pos
        self.zone = (zone or "").upper() or None
        self.zones = {}            # サーバconfig: zone -> delay_ms
        self.base_ms = 0.0         # ハウスPA位相合わせトリム
        self.gl, self.gr = PAN.get(pos.upper(), (0.7071, 0.7071))
        self.ring = np.zeros((RING, 2), dtype=np.float32)
        self.lock = threading.Lock()
        self.t0_stream = None      # stream time (s) at first callback
        self.t0_wall = None        # wall clock (s) at first callback
        self.epoch_off = None      # server_epoch = local_wall + epoch_off
        self.stats = {"frames": 0, "late": 0, "under": 0}

    # ---- sounddevice callback (audio thread) ----
    def callback(self, outdata, nframes, t, status):
        if self.t0_stream is None:
            self.t0_stream = t.outputBufferDacTime
            self.t0_wall = time.time()
        start = int(round(t.outputBufferDacTime * SR))
        idx = np.arange(start, start + nframes) % RING
        with self.lock:
            block = self.ring[idx].copy()
            self.ring[idx] = 0.0           # consume (avoid stale replay)
        outdata[:] = block

    # ---- wall<->stream mapping ----
    def stream_sample_for_wall(self, wall):
        return int(round((self.t0_stream + (wall - self.t0_wall)) * SR))

    def delay_sec(self):
        z = self.zones.get(self.zone, 0.0) if self.zone else 0.0
        return max(0.0, (z + self.base_ms) / 1000.0)

    def schedule(self, mono_f32, play_at):
        delay = self.delay_sec()
        eo, t0s, t0w = self.epoch_off, self.t0_stream, self.t0_wall
        if t0s is None or t0w is None or eo is None:
            return
        target_wall = play_at + delay - eo
        start = self.stream_sample_for_wall(target_wall)
        now_stream = int(round((t0s + (time.time() - t0w)) * SR))
        if start < now_stream + 64:        # too late to place in sync
            self.stats["late"] += 1
            return
        n = len(mono_f32)
        idx = np.arange(start, start + n) % RING
        with self.lock:
            self.ring[idx, 0] += mono_f32 * self.gl
            self.ring[idx, 1] += mono_f32 * self.gr
        self.stats["frames"] += 1


async def net(player, server, ch):
    url = f"{server}/audio?role=listen&ch={ch}&pos={player.pos}"
    if player.zone:
        url += f"&zone={player.zone}"
    best_rtt = float("inf")
    async with websockets.connect(url, max_size=None) as ws:
        async def pinger():
            while True:
                await ws.send(json.dumps({"t": "ping", "c": time.time() * 1000}))
                await asyncio.sleep(0.5)
        ping_task = asyncio.create_task(pinger())
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    m = json.loads(msg)
                    if m.get("t") == "config":
                        player.zones = {k.upper(): float(v)
                                        for k, v in (m.get("zones") or {}).items()}
                        player.base_ms = float(m.get("base_ms", 0.0))
                        if int(m.get("sr", SR)) != SR:
                            print(f"[play {player.pos}] ⚠ source sr={m['sr']} != {SR}: "
                                  f"ノード運用は48kHz送出(source.py)が前提。ピッチが狂います")
                        print(f"[play {player.pos}] config zone={player.zone} "
                              f"delay={player.delay_sec()*1000:.1f}ms")
                    elif m.get("t") == "pong":
                        now = time.time() * 1000
                        rtt = now - m["c"]
                        nonlocal_best = rtt < best_rtt
                        if nonlocal_best or player.epoch_off is None:
                            best_rtt = min(best_rtt, rtt)
                            server_at_recv = m["s"] + rtt / 2          # ms
                            # server_epoch(s) = local_wall(s) + epoch_off
                            player.epoch_off = (server_at_recv - now) / 1000.0
                else:
                    if len(msg) <= HEADER.size:
                        continue
                    _, _, nchan, _p, seq, nsamp, play_at = HEADER.unpack_from(msg, 0)
                    pcm = np.frombuffer(msg, dtype="<i2", count=nsamp, offset=HEADER.size)
                    player.schedule(pcm.astype(np.float32) / 32768.0, play_at)
        finally:
            ping_task.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pos", choices=["L", "C", "R", "l", "c", "r"])
    ap.add_argument("--server", default="ws://127.0.0.1:8900")
    ap.add_argument("--ch", default="festival")
    ap.add_argument("--zone", help="ゾーン名(A..F等)。サーバのzone表の遅延を適用")
    a = ap.parse_args()
    player = Player(a.pos.upper(), zone=a.zone)

    stream = sd.OutputStream(samplerate=SR, channels=2, dtype="float32",
                             blocksize=480, callback=player.callback)
    stream.start()
    print(f"[play {player.pos}] speaker live (pan L={player.gl} R={player.gr})")

    def status_loop():
        while True:
            time.sleep(2)
            s = player.stats
            print(f"[play {player.pos}] frames={s['frames']} late={s['late']} "
                  f"epoch_off={'%.3f' % player.epoch_off if player.epoch_off else '—'}s")
    threading.Thread(target=status_loop, daemon=True).start()

    try:
        asyncio.run(net(player, a.server, a.ch))
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop(); stream.close()


if __name__ == "__main__":
    main()
