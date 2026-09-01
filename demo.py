#!/usr/bin/env python3
"""
SOLUNA Sound — 60-second demo.

    python3 demo.py

Starts the server, generates an ambient demo loop, fires it as a synced cue
with a plasma light show, and prints the URL. Open it on two phones on the
same Wi-Fi, tap ▶ on both, and hear (and see) them lock together.
"""
import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8900"))
ADMIN = os.environ.get("SOLUNA_ADMIN", "demo")
SR = 48000


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def write_demo_wav(path, seconds=16):
    """Warm ambient loop: slow chord swells + a soft pulse, loops cleanly."""
    n = SR * seconds
    frames = bytearray()
    chord = [130.81, 196.00, 261.63, 329.63]          # C3 G3 C4 E4
    for i in range(n):
        t = i / SR
        swell = 0.5 - 0.5 * math.cos(2 * math.pi * t / seconds)   # ループ滑らか
        v = sum(math.sin(2 * math.pi * f * t) * 0.09 for f in chord) * (0.35 + 0.65 * swell)
        pulse_ph = (t * 2) % 1.0                       # 120bpm の柔らかい鼓動
        if pulse_ph < 0.18:
            env = math.sin(pulse_ph / 0.18 * math.pi)
            v += 0.22 * env * math.sin(2 * math.pi * 523.25 * t)
        frames += struct.pack("<h", int(max(-1, min(1, v)) * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))


def post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}?ch=festival",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-soluna-admin": ADMIN})
    return urllib.request.urlopen(req).status


def main():
    os.makedirs(os.path.join(HERE, "assets"), exist_ok=True)
    demo_wav = os.path.join(HERE, "assets", "demo.wav")
    if not os.path.exists(demo_wav):
        print("… generating demo loop (assets/demo.wav)")
        write_demo_wav(demo_wav)

    env = {**os.environ, "PORT": str(PORT), "SOLUNA_ADMIN": ADMIN}
    server = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env)
    try:
        for _ in range(50):                            # wait for /status
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        post("/api/cue", {"url": "/assets/demo.wav", "lead": 3, "gain": 1.0, "loop": True})
        post("/api/light", {"pattern": "audio", "colors": ["#d4af37", "#7fc9a2"],
                            "brightness": 1.0})
        ip = lan_ip()
        print(f"""
🔊🌈 SOLUNA Sound demo is live — open on two phones (same Wi-Fi):

      http://{ip}:{PORT}/

   Tap ▶ on both. The loop and the light show lock together.
   DJ from your laptop:  http://{ip}:{PORT}/dj
   FOH console:          http://{ip}:{PORT}/admin   (token: {ADMIN})

   Ctrl-C to stop.
""")
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
