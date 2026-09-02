#!/usr/bin/env python3
"""
SOLUNA Surround source — pushes a 3-channel (L / R / C) PCM stream in real time.

Modes:
  --test            generate a demo bed: soft pad + a blip that rotates L->C->R->C
                    so you can HEAR the sound move across the 3 devices.
  --file song.mp3   decode via ffmpeg to 48k stereo, derive 3 positional channels
                    (L=left, R=right, C=mid). Add --rotate to overlay a moving blip.
  --input [dev]     LIVE: オーディオIF入力(=既存ハウスPAのFOH matrix/aux out)を
                    リアルタイム送出。--lead 0.08 と併用してハウスPAと融合する。
                    dev省略=デフォルト入力。一覧: python3 -c "import sounddevice;print(sounddevice.query_devices())"
  --aes67 ADDR:PORT LIVE: AES67 / Ravenna / Dante(AES67モード) のRTPマルチキャストを直接受信
                    (L24/L16・48k・1ms/125µs)。既存の音響ネットワークにケーブル1本で乗る。
                    --aes67 auto = SAPで見つかった最初のストリーム / --aes67 "名前の一部" で選択。
  --sap             SAP(239.255.255.255:9875)を数秒聞いて、流れているAES67/Danteストリームを一覧表示
  --map SPEC        入力ch→L/R/Cの割り当て。既定 "L=1,R=2,C=1+2"(C=1と2の平均)

Usage:
  python3 source.py --test  --ch festival --server ws://192.168.0.194:8900
  python3 source.py --file ~/Music/x.mp3 --ch festival
  python3 source.py --input --lead 0.08 --ch festival      # FOHフィード融合
  python3 source.py --sap                                     # 何が流れているか見る
  python3 source.py --aes67 239.69.1.40:5004 --lead 0.08     # Dante/AES67フローを直接受ける
  python3 source.py --aes67 auto --map "L=3,R=4,C=3+4"       # SAPで自動選択・ch3/4を使う
"""
import argparse
import asyncio
import json
import math
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
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


# ---- AES67 / Ravenna / Dante(AES67 mode) ingest --------------------------------
# 既存フェスの音響ネットワーク(Dante/AES67)に「もう1台の受信機」として乗るための受け口。
# RTP(RFC3550)+L24/L16(RFC3190)を受け、20msのSL2フレームに詰め直して server へ push する。
# PTPは要らない: SOLUNAの playAt はサーバ側がサンプル数から刻む(=送り手の時計に依存しない)。
# 受信側で必要なのは「サンプルが欠けたら同じ数だけ無音を詰める」ことだけ。それを RtpReassembler がやる。
SAP_ADDR, SAP_PORT = "239.255.255.255", 9875
RTP_HDR = struct.Struct("!BBHII")
AES67_FMT = {"L24": 3, "L16": 2}
MAX_GAP_SAMPLES = SR // 2               # 0.5s 超の穴は「別ストリーム」とみなして再同期


def parse_map(spec):
    """'L=1,R=2,C=1+2' → {'L':[1],'R':[2],'C':[1,2]} (1始まりの入力ch番号。+ は平均)。"""
    spec = (spec or "L=1,R=2,C=1+2").replace(" ", "")
    out = {}
    for part in spec.split(","):
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"--map: '{part}' は POS=ch[+ch] の形で")
        pos, chs = part.split("=", 1)
        pos = pos.upper()
        if pos not in POSMAP:
            raise ValueError(f"--map: 位置は L/R/C のみ ('{pos}')")
        try:
            out[pos] = [int(c) for c in chs.split("+") if c]
        except ValueError:
            raise ValueError(f"--map: ch番号が数字でない ('{chs}')")
        if not out[pos] or min(out[pos]) < 1:
            raise ValueError(f"--map: ch番号は1以上 ('{part}')")
    for pos in POSMAP:
        out.setdefault(pos, {"L": [1], "R": [2], "C": [1, 2]}[pos])
    return out


def apply_map(block, mapping):
    """block: (N, nch) float32 → (N, 3) float32 [L,R,C]。存在しないchは無音扱い。"""
    n, nch = block.shape
    out = np.zeros((n, 3), dtype=np.float32)
    for i, pos in enumerate(POSMAP):
        chs = [c - 1 for c in mapping[pos] if 0 < c <= nch]
        if chs:
            out[:, i] = block[:, chs].mean(axis=1)
    return out


def _pcm_to_f32(payload, fmt, nch):
    """RTP payload(ビッグエンディアン L24/L16, ch interleaved) → (N, nch) float32。"""
    bps = AES67_FMT[fmt]
    frame_bytes = bps * nch
    usable = (len(payload) // frame_bytes) * frame_bytes
    if usable == 0:
        return np.zeros((0, nch), dtype=np.float32)
    if bps == 3:
        b = np.frombuffer(payload[:usable], dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
        v = (b[:, 0] << 24) | (b[:, 1] << 16) | (b[:, 2] << 8)     # 上位24bitに置いて
        v = v.astype(np.int32) >> 8                                  # 算術シフトで符号復元
        f = v.astype(np.float32) / 8388608.0
    else:
        f = np.frombuffer(payload[:usable], dtype=">i2").astype(np.float32) / 32768.0
    return f.reshape(-1, nch)


class RtpReassembler:
    """RTPパケット列 → 連続サンプル列。timestampをサンプルカウンタとして使い、
    欠落は無音で埋め、重複/遅着は捨て、seq/timestamp の 32/16bit 折り返しも扱う。"""

    def __init__(self, fmt="L24", nch=2):
        if fmt not in AES67_FMT:
            raise ValueError("fmt は L24 か L16")
        self.fmt, self.nch = fmt, nch
        self.expect_ts = None          # 次に来るべき timestamp
        self.last_seq = None
        self.ssrc = None
        self.buf = []                  # list of (N, nch) float32
        self.buffered = 0
        self.stats = {"pkts": 0, "gaps": 0, "filled": 0, "dropped": 0, "resync": 0}

    def feed(self, pkt):
        if len(pkt) < RTP_HDR.size:
            return
        b0, b1, seq, ts, ssrc = RTP_HDR.unpack_from(pkt, 0)
        if (b0 >> 6) != 2:              # RTP v2 以外は無視
            return
        off = RTP_HDR.size + 4 * (b0 & 0x0F)            # CSRC
        if b0 & 0x10:                                    # header extension
            if len(pkt) < off + 4:
                return
            ext_len = struct.unpack_from("!HH", pkt, off)[1]
            off += 4 + 4 * ext_len
        end = len(pkt)
        if b0 & 0x20:                                    # padding
            end -= pkt[-1]
        if end <= off:
            return
        if self.ssrc is not None and ssrc != self.ssrc:  # 別ソースに切り替わった
            self._resync()
        self.ssrc = ssrc
        block = _pcm_to_f32(pkt[off:end], self.fmt, self.nch)
        n = block.shape[0]
        if n == 0:
            return
        self.stats["pkts"] += 1
        if self.expect_ts is not None:
            delta = (ts - self.expect_ts) & 0xFFFFFFFF
            if delta >= 0x80000000:                      # 負=遅着/重複 → 捨てる
                self.stats["dropped"] += 1
                return
            if 0 < delta <= MAX_GAP_SAMPLES:              # 穴 → 同じサンプル数の無音
                self._push(np.zeros((delta, self.nch), dtype=np.float32))
                self.stats["gaps"] += 1
                self.stats["filled"] += delta
            elif delta > MAX_GAP_SAMPLES:                 # 大穴=ストリーム再開扱い
                self.stats["resync"] += 1
        self._push(block)
        self.expect_ts = (ts + n) & 0xFFFFFFFF
        self.last_seq = seq

    def _resync(self):
        self.expect_ts = None
        self.stats["resync"] += 1

    def _push(self, block):
        self.buf.append(block)
        self.buffered += block.shape[0]

    def pop(self, n=FRAME):
        """n サンプルたまっていれば (n, nch) を返す。無ければ None。"""
        if self.buffered < n:
            return None
        chunks, need = [], n
        while need > 0:
            b = self.buf[0]
            if b.shape[0] <= need:
                chunks.append(b)
                need -= b.shape[0]
                self.buf.pop(0)
            else:
                chunks.append(b[:need])
                self.buf[0] = b[need:]
                need = 0
        self.buffered -= n
        return np.concatenate(chunks, axis=0)


def parse_sdp(text):
    """SDP → {'name','addr','port','fmt','sr','ch','ptime'} (音声mでないと None)。
    Dante/Ravenna の a=ts-refclk / a=mediaclk / a=recvonly 等は読み飛ばす。"""
    if not text or "v=0" not in text:
        return None
    text = text[text.index("v=0"):]
    info = {"name": "", "addr": None, "port": None, "fmt": None, "sr": 48000, "ch": 2,
            "ptime": None, "pt": None}
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if line.startswith("s="):
            info["name"] = line[2:].strip()
        elif line.startswith("c=IN IP4 "):
            info["addr"] = line[9:].split("/")[0].strip()
        elif line.startswith("m=audio "):
            parts = line.split()
            try:
                info["port"] = int(parts[1])
            except (IndexError, ValueError):
                return None
            info["pt"] = parts[3] if len(parts) > 3 else None
        elif line.startswith("m="):
            return None                                  # 音声以外
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+)\s+(L16|L24)/(\d+)(?:/(\d+))?", line, re.I)
            if m and (info["pt"] is None or m.group(1) == info["pt"]):
                info["fmt"] = m.group(2).upper()
                info["sr"] = int(m.group(3))
                info["ch"] = int(m.group(4) or 1)
        elif line.startswith("a=ptime:"):
            try:
                info["ptime"] = float(line[8:])
            except ValueError:
                pass
    if not info["addr"] or not info["port"] or not info["fmt"]:
        return None
    return info


def parse_sap(pkt):
    """SAP(RFC2974) パケット → SDP文字列(なければ None)。"""
    if len(pkt) < 8:
        return None
    flags, auth_len = pkt[0], pkt[1]
    if (flags >> 5) != 1:                                # version 1 のみ
        return None
    if flags & 0x04:                                     # deletion
        return None
    off = 8 if not (flags & 0x10) else 20                # A=IPv6 origin
    off += 4 * auth_len
    body = pkt[off:]
    if flags & 0x02:                                     # compressed(zlib)
        import zlib
        try:
            body = zlib.decompress(body)
        except zlib.error:
            return None
    if body.startswith(b"application/sdp\0"):
        body = body[len(b"application/sdp\0"):]
    elif b"v=0" in body:
        body = body[body.index(b"v=0"):]
    else:
        return None
    return body.decode("utf-8", "replace")


def open_udp(addr, port, iface=None):
    """UDP受信ソケット。addr がマルチキャストなら join(iface=受信NICのIPv4)。ユニキャストも受ける。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("", port))
    first = int(addr.split(".")[0]) if addr and addr[0].isdigit() else 0
    if 224 <= first <= 239:
        mreq = socket.inet_aton(addr) + socket.inet_aton(iface or "0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


def sap_listen(seconds=4.0, iface=None):
    """SAPを seconds 秒聞き、見つかった音声ストリームを [dict] で返す(同じaddr:portは1回)。"""
    found = {}
    try:
        sock = open_udp(SAP_ADDR, SAP_PORT, iface)
    except OSError as e:
        print(f"[sap] listen failed: {e}", file=sys.stderr)
        return []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        r, _, _ = select.select([sock], [], [], 0.2)
        if not r:
            continue
        try:
            pkt, _ = sock.recvfrom(65535)
        except OSError:
            continue
        sdp = parse_sap(pkt)
        info = parse_sdp(sdp) if sdp else None
        if info:
            found[(info["addr"], info["port"])] = info
    sock.close()
    return list(found.values())


def print_streams(streams):
    if not streams:
        print("[sap] no AES67/Dante streams announced (Danteは端末側でAES67モード+マルチキャストフロー作成が必要)")
        return
    print(f"{'name':32} {'addr:port':22} {'fmt':4} {'sr':6} {'ch':3} ptime")
    for s_ in streams:
        print(f"{s_['name'][:32]:32} {s_['addr']+':'+str(s_['port']):22} {s_['fmt']:4} "
              f"{s_['sr']:6} {s_['ch']:3} {s_['ptime'] if s_['ptime'] is not None else '-'}")


def resolve_aes67(spec, iface=None, sap_seconds=4.0):
    """--aes67 の指定 → (addr, port, info|None)。'auto' / 名前の一部 は SAP から選ぶ。"""
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+):(\d+)$", spec or "")
    if m:
        return m.group(1), int(m.group(2)), None
    print(f"[sap] listening {sap_seconds:.0f}s for '{spec}' …")
    streams = sap_listen(sap_seconds, iface)
    print_streams(streams)
    if not streams:
        raise SystemExit("[aes67] no stream found via SAP — specify ADDR:PORT instead")
    if spec == "auto":
        pick = streams[0]
    else:
        cands = [s_ for s_ in streams if spec.lower() in s_["name"].lower()]
        if not cands:
            raise SystemExit(f"[aes67] no stream name contains '{spec}'")
        pick = cands[0]
    print(f"[aes67] using '{pick['name']}' {pick['addr']}:{pick['port']} {pick['fmt']} ch={pick['ch']}")
    return pick["addr"], pick["port"], pick


async def aes67_loop(ws, args, seq_start=0):
    """AES67 受信 → 20ms SL2 push。サンプル欠落は無音で埋める(位相は崩さない)。"""
    addr, port, info = resolve_aes67(args.aes67, args.iface, args.sap_seconds)
    fmt = (args.aes67_fmt or (info and info["fmt"]) or "L24").upper()
    nch = args.aes67_ch or (info and info["ch"]) or 2
    if info and info["sr"] != SR:
        print(f"[aes67] WARNING: stream sr={info['sr']} ≠ {SR} (リサンプル未対応・そのまま送る)")
    mapping = parse_map(args.map)
    rx = RtpReassembler(fmt, nch)
    sock = open_udp(addr, port, args.iface)
    loop = asyncio.get_event_loop()
    print(f"[aes67] rx {addr}:{port} {fmt} ch={nch} map={mapping} "
          f"lead={args.lead or 'server default'} — Ctrl-C to stop.", flush=True)
    seq, last_log, sent = seq_start, time.monotonic(), 0
    while True:
        try:
            pkt = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=5.0)
        except asyncio.TimeoutError:
            print("[aes67] no packets for 5s — waiting (flow stopped? wrong iface?)", flush=True)
            continue
        rx.feed(pkt)
        while True:
            block = rx.pop(FRAME)
            if block is None:
                break
            i16 = (np.clip(apply_map(block, mapping), -1, 1) * 32767).astype(np.int16)
            await ws.send(pack_frame(seq, i16))
            seq += 1
            sent += 1
        if time.monotonic() - last_log > 10:
            st = rx.stats
            print(f"[aes67] frames={sent} pkts={st['pkts']} gaps={st['gaps']} "
                  f"filled={st['filled']} dropped={st['dropped']} resync={st['resync']}", flush=True)
            last_log = time.monotonic()


async def run(args):
    url = f"{args.server}/audio?role=push&ch={args.ch}"
    tok = os.environ.get("SOLUNA_TOKEN")
    if tok:                       # koe* チャンネルのトークンゲート対応
        url += f"&token={tok}"
    async with websockets.connect(url, max_size=None) as ws:
        hello = {"t": "hello", "map": POSMAP, "sr": SR}
        if args.lead is not None:
            hello["lead"] = args.lead
        await ws.send(json.dumps(hello))
        safe = url.split("&token=")[0]      # トークンをログに出さない
        print(f"[source] connected {safe} map={POSMAP}")

        seq = 0
        loop = asyncio.get_event_loop()
        start = loop.time()

        if getattr(args, "aes67", None):
            await aes67_loop(ws, args)
        elif args.input is not None:
            import queue as _queue
            import sounddevice as sd
            q: "_queue.Queue" = _queue.Queue(maxsize=64)

            def cb(indata, nframes, t, status):
                if status:
                    print(f"[source] input status: {status}", file=sys.stderr)
                try:
                    q.put_nowait(indata.copy())
                except _queue.Full:
                    pass                    # 送出が詰まったら古い方を捨てる側で吸収

            dev = args.input if args.input != "" else None
            try:
                dev = int(dev) if dev is not None else None
            except ValueError:
                pass
            with sd.InputStream(samplerate=SR, channels=2, dtype="float32",
                                blocksize=FRAME, device=dev, callback=cb):
                print(f"[source] LIVE input dev={dev or 'default'} "
                      f"lead={args.lead or 'server default'} — Ctrl-C to stop.")
                while True:
                    block2 = await asyncio.to_thread(q.get)
                    Lc, Rc = block2[:, 0], block2[:, 1]
                    block = np.stack([Lc, Rc, 0.5 * (Lc + Rc)], axis=1)
                    i16 = (np.clip(block, -1, 1) * 32767).astype(np.int16)
                    await ws.send(pack_frame(seq, i16))
                    seq += 1
        elif args.file:
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
    ap.add_argument("--input", nargs="?", const="",
                    help="ライブ入力デバイス(名前/番号。省略=デフォルト)")
    ap.add_argument("--lead", type=float,
                    help="サーバ先読み秒(有線LAN+ハウスPA融合=0.08推奨)")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--aes67", metavar="ADDR:PORT|auto|NAME",
                    help="AES67/Ravenna/Dante(AES67モード)のRTPを受信 (auto/名前=SAPで選択)")
    ap.add_argument("--aes67-fmt", choices=["L24", "L16"], help="ペイロード形式(既定 L24 / SAPから)")
    ap.add_argument("--aes67-ch", type=int, help="ストリームのch数(既定 2 / SAPから)")
    ap.add_argument("--iface", help="マルチキャスト受信NICのIPv4(複数NICのとき)")
    ap.add_argument("--map", help='入力ch→L/R/C 例 "L=1,R=2,C=1+2"(既定)')
    ap.add_argument("--sap", action="store_true", help="SAPを聞いてAES67/Danteストリーム一覧を表示して終了")
    ap.add_argument("--sap-seconds", type=float, default=4.0)
    a = ap.parse_args()
    if a.sap:
        print_streams(sap_listen(a.sap_seconds, a.iface))
        sys.exit(0)
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("\n[source] stopped")
