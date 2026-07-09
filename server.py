#!/usr/bin/env python3
"""
SOLUNA Surround server (v2) — clock-synced, position-routed audio fanout.

Extends the original SOLUNA audio protocol (mono broadcast over /ws/soluna-audio)
into a real multi-device surround system:

  * One PUSH source sends interleaved N-channel PCM (48kHz s16le).
  * Each LISTEN device claims a position (L / R / C / ...). The server extracts
    only that device's channel and forwards a mono frame.
  * Every forwarded frame carries `playAt` = a server-clock epoch (seconds) at
    which sample 0 of that frame must be heard. playAt is driven by a running
    sample counter, NOT by arrival time, so network jitter never shifts it.
  * Listeners run a ping/pong clock-sync against the server, so all devices map
    the SAME playAt to the SAME wall-clock instant -> phase-aligned surround.

Wire format (binary, little-endian), header = 22 bytes:
    magic   3s   b"SL2"
    version B    = 2
    nchan   B    channels in THIS frame (1 after server extraction)
    pad     B    0
    seq     I    frame sequence (from source)
    nsamp   I    samples per channel in this frame
    playAt  d    server-epoch seconds for sample 0 (server-filled; 0 from source)
  payload: int16 interleaved PCM, nchan * nsamp samples.

HTTP:
    GET /                 -> player (client.html), ?pos=L|R|C
    GET /status           -> JSON channel/listener state (for verification)
    WS  /audio?role=push&ch=<name>     (source; first sends JSON hello {map,sr})
    WS  /audio?role=listen&ch=<name>&pos=L
"""
import asyncio
import json
import struct
import time
import os
from aiohttp import web, WSMsgType

SR = 48000
LEAD = 0.6                      # seconds: schedule playback this far ahead of "now"
HEADER = struct.Struct("<3sBBBIId")
HERE = os.path.dirname(os.path.abspath(__file__))

# channel_name -> state
channels: dict = {}


def _chan(name):
    return channels.setdefault(name, {
        "source": None,           # push ws
        "map": ["L", "R", "C"],   # position -> index order
        "sr": SR,
        "epoch": None,            # server-epoch for sample 0 of the stream
        "played": 0,              # cumulative samples emitted
        "seq": 0,
        "listeners": {},          # ws -> pos
    })


def _pos_index(state, pos):
    m = state["map"]
    pos = (pos or "").upper()
    if pos in m:
        return m.index(pos)
    # numeric fallback
    try:
        i = int(pos)
        if 0 <= i < len(m):
            return i
    except (ValueError, TypeError):
        pass
    return 0


async def audio_ws(request):
    role = request.query.get("role", "listen")
    name = request.query.get("ch", "default")
    # koe* チャンネルは声コマンドに直結する(ブリッジがClaude Codeへタイプ)ため、
    # SOLUNA_TOKEN が設定されていればトークン必須。それ以外のchは従来どおり素通し。
    tok = os.environ.get("SOLUNA_TOKEN")
    if tok and name.startswith("koe") and request.query.get("token") != tok:
        raise web.HTTPForbidden(text="token required for koe* channels")
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    state = _chan(name)

    if role == "push":
        state["source"] = ws
        state["epoch"] = None
        state["played"] = 0
        state["seq"] = 0
        print(f"[push] source connected ch={name}")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        hello = json.loads(msg.data)
                        if hello.get("t") == "hello":
                            state["map"] = [p.upper() for p in hello.get("map", state["map"])]
                            state["sr"] = int(hello.get("sr", SR))
                            print(f"[push] ch={name} map={state['map']} sr={state['sr']}")
                    except Exception:
                        pass
                elif msg.type == WSMsgType.BINARY:
                    await _fanout(name, state, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if state.get("source") is ws:
                state["source"] = None
            print(f"[push] source gone ch={name}")
        return ws

    # role == listen
    pos = request.query.get("pos", "L")
    state["listeners"][ws] = pos
    print(f"[listen] +{pos} ch={name} (n={len(state['listeners'])})")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get("t") == "ping":
                    # echo client time + stamp server epoch (ms) for clock sync
                    await ws.send_str(json.dumps({
                        "t": "pong", "c": m.get("c"), "s": time.time() * 1000.0
                    }))
                elif m.get("t") == "pos":
                    state["listeners"][ws] = m.get("pos", pos)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        state["listeners"].pop(ws, None)
        print(f"[listen] -{pos} ch={name} (n={len(state['listeners'])})")
    return ws


async def _fanout(name, state, data: bytes):
    if len(data) < HEADER.size:
        return
    magic, ver, nchan, _pad, seq, nsamp, _playat = HEADER.unpack_from(data, 0)
    if magic != b"SL2" or nchan < 1 or nsamp < 1:
        return
    body = memoryview(data)[HEADER.size:]
    total = nchan * nsamp
    # int16 view of payload
    import array
    pcm = array.array("h")
    pcm.frombytes(body[: total * 2].tobytes())
    if len(pcm) < total:
        return

    now = time.time()
    if state["epoch"] is None:
        state["epoch"] = now + LEAD
        state["played"] = 0
    play_at = state["epoch"] + state["played"] / float(state["sr"])
    state["played"] += nsamp
    state["seq"] = seq

    # Build one mono frame per distinct position in use, then send.
    cache = {}
    dead = []
    for ws, pos in list(state["listeners"].items()):
        idx = _pos_index(state, pos)
        if idx >= nchan:
            idx = 0
        frame = cache.get(idx)
        if frame is None:
            mono = pcm[idx::nchan]           # de-interleave this channel
            hdr = HEADER.pack(b"SL2", 2, 1, 0, seq, nsamp, play_at)
            frame = hdr + mono.tobytes()
            cache[idx] = frame
        try:
            await ws.send_bytes(frame)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state["listeners"].pop(ws, None)


async def status(request):
    out = {}
    for name, st in channels.items():
        out[name] = {
            "source": st["source"] is not None,
            "map": st["map"],
            "sr": st["sr"],
            "seq": st["seq"],
            "played_samples": st["played"],
            "stream_t": (None if st["epoch"] is None
                         else round(st["played"] / float(st["sr"]), 2)),
            "listeners": sorted(st["listeners"].values()),
        }
    return web.json_response({"server_epoch_ms": time.time() * 1000.0,
                              "lead": LEAD, "channels": out})


async def index(request):
    return web.FileResponse(os.path.join(HERE, "client.html"))


async def mic(request):
    # koe-claude 用: iPhone/どの端末でもマイクをリアルタイム送信する送話ページ
    return web.FileResponse(os.path.join(HERE, "mic.html"))


def main():
    port = int(os.environ.get("PORT", "8900"))
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/mic", mic),
        web.get("/status", status),
        web.get("/audio", audio_ws),
    ])
    ip = os.environ.get("LAN_IP", "192.168.0.194")
    print(f"\n🔊 SOLUNA Surround  http://{ip}:{port}/")
    print(f"   L  http://{ip}:{port}/?pos=L")
    print(f"   R  http://{ip}:{port}/?pos=R")
    print(f"   C  http://{ip}:{port}/?pos=C\n")
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
