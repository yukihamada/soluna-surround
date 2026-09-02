#!/usr/bin/env python3
"""source.py --aes67 (AES67/Ravenna/Dante-AES67 受信) の検証。
SDP/SAP パーサ・--map・RTP再構成(穴埋め/折り返し/重複) の単体 + 合成RTP送信→source.py→server→
リスナーWSでSL2フレームが正しいレベルで届く統合テスト。音デバイス不要・マルチキャスト不要(ユニキャストで検証)。
  python3 tests/test_aes67.py"""
import asyncio, json, os, socket, struct, subprocess, sys, threading, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import source
from source import (RtpReassembler, parse_map, apply_map, parse_sdp, parse_sap, RTP_HDR, SR, FRAME,
                    HEADER, _pcm_to_f32)
from _server import ServerProc, PORT, DATA_DIR

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name); print(f"  {'✅' if cond else '❌'} {name} {detail}")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 1) --map ---------------------------------------------------------------------
m = parse_map(None)
check("map 既定 L=1,R=2,C=1+2", m == {"L": [1], "R": [2], "C": [1, 2]}, str(m))
m = parse_map("L=3, R=4, C=3+4")
check("map 空白許容・ch3/4", m == {"L": [3], "R": [4], "C": [3, 4]}, str(m))
m = parse_map("c=5")
check("map 部分指定は残りが既定", m == {"L": [1], "R": [2], "C": [5]}, str(m))
for bad in ("X=1", "L=0", "L=a", "L1"):
    try:
        parse_map(bad); check(f"map 不正 '{bad}' は例外", False)
    except ValueError:
        check(f"map 不正 '{bad}' は例外", True)
blk = np.zeros((4, 4), dtype=np.float32); blk[:, 2] = 0.2; blk[:, 3] = 0.6
out = apply_map(blk, parse_map("L=3,R=4,C=3+4"))
check("apply_map: L=ch3 R=ch4 C=平均", np.allclose(out[0], [0.2, 0.6, 0.4]), str(out[0]))
out = apply_map(blk, parse_map("L=9"))
check("apply_map: 無いchは無音", float(np.abs(out[:, 0]).max()) == 0.0)

# ---- 2) L24/L16 decode ----------------------------------------------------------
def l24(vals):   # 24bit signed big-endian
    return b"".join(int(v).to_bytes(3, "big", signed=True) for v in vals)
f = _pcm_to_f32(l24([8388607, -8388608, 0, 4194304]), "L24", 2)
check("L24 decode 最大/最小/0/半分", f.shape == (2, 2) and np.allclose(f.reshape(-1), [8388607/8388608, -1.0, 0.0, 0.5], atol=1e-6), str(f.reshape(-1)))
f = _pcm_to_f32(struct.pack(">4h", 32767, -32768, 0, 16384), "L16", 2)
check("L16 decode", np.allclose(f.reshape(-1), [32767/32768, -1.0, 0.0, 0.5]), str(f.reshape(-1)))
f = _pcm_to_f32(b"\x00" * 7, "L24", 2)
check("L24 端数バイトは切り捨て", f.shape == (1, 2))

# ---- 3) RTP reassembly ----------------------------------------------------------
def rtp(seq, ts, payload, ssrc=0x1234, pt=98, marker=0, csrc=0, ext=None, pad=0):
    b0 = 0x80 | (0x10 if ext is not None else 0) | (0x20 if pad else 0) | csrc
    hdr = RTP_HDR.pack(b0, (marker << 7) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    hdr += b"\0\0\0\0" * csrc
    if ext is not None:
        hdr += struct.pack("!HH", 0xBEDE, len(ext) // 4) + ext
    body = payload
    if pad:
        body += b"\0" * (pad - 1) + bytes([pad])
    return hdr + body
def pkt48(seq, ts, value, **kw):    # 48 samples × 2ch of constant value(24bit)
    return rtp(seq, ts, l24([value, value] * 48), **kw)

rx = RtpReassembler("L24", 2)
for i in range(20):
    rx.feed(pkt48(i, 1000 + 48 * i, 1000000))
check("reasm: 20pkt×48 = 960 → pop 1フレーム", rx.pop(FRAME) is not None and rx.pop(FRAME) is None and rx.stats["pkts"] == 20)

rx = RtpReassembler("L24", 2)
rx.feed(pkt48(0, 0, 4194304))
rx.feed(pkt48(3, 144, 4194304))          # seq 1,2 (96 samples) 欠落
blk = rx.pop(192)
check("reasm: 欠落96サンプルを無音で埋める", blk is not None and rx.stats["gaps"] == 1 and rx.stats["filled"] == 96
      and np.allclose(blk[:48, 0], 0.5) and np.all(blk[48:144] == 0) and np.allclose(blk[144:, 0], 0.5))
rx.feed(pkt48(2, 96, 4194304))           # 遅着 → 捨てる
check("reasm: 遅着パケットは捨てる", rx.stats["dropped"] == 1 and rx.buffered == 0)
rx.feed(pkt48(3, 144, 4194304))          # 重複 → 捨てる
check("reasm: 重複パケットは捨てる", rx.stats["dropped"] == 2 and rx.buffered == 0)

rx = RtpReassembler("L24", 2)
rx.feed(pkt48(0xFFFF, 0xFFFFFFFF - 47, 100))
rx.feed(pkt48(0, 0, 100))                # ts/seq 折り返し(ちょうど連続: …FFD0+48 = 0)
check("reasm: timestamp 2^32 折り返しで穴なし", rx.stats["gaps"] == 0 and rx.buffered == 96 and rx.expect_ts == 48)
rx.feed(pkt48(1, 96, 100))               # 折り返し直後に48サンプルの穴
check("reasm: 折り返し直後の穴も埋める", rx.stats["gaps"] == 1 and rx.stats["filled"] == 48 and rx.buffered == 192)

rx = RtpReassembler("L24", 2)
rx.feed(pkt48(0, 0, 100)); rx.feed(pkt48(1, SR * 5, 100))   # 5秒の大穴 → 再同期(埋めない)
check("reasm: 0.5s超の穴は再同期扱い(無音で埋めない)", rx.stats["resync"] == 1 and rx.buffered == 96)

rx = RtpReassembler("L24", 2)
rx.feed(pkt48(0, 0, 100)); rx.feed(pkt48(1, 48, 100, ssrc=0x9999))  # SSRC切替 → 再同期
check("reasm: SSRC変化で再同期", rx.stats["resync"] == 1 and rx.buffered == 96)

rx = RtpReassembler("L24", 2)
rx.feed(pkt48(0, 0, 4194304, csrc=1, ext=b"\x01\x02\x03\x04", pad=4))
b = rx.pop(48)
check("reasm: CSRC+拡張ヘッダ+パディングを正しく剥がす", b is not None and np.allclose(b[:, 0], 0.5))
rx.feed(b"\x00" * 20)
check("reasm: RTP v2以外は無視", rx.stats["pkts"] == 1)

rx = RtpReassembler("L16", 8)            # 125µs × 8ch = 6サンプル/pkt
for i in range(160):
    rx.feed(rtp(i, 6 * i, struct.pack(">48h", *([8192] * 48))))
b = rx.pop(FRAME)
check("reasm: L16 8ch 125µs パケット(6サンプル)×160 = 960", b is not None and b.shape == (960, 8) and np.allclose(b, 0.25))

# ---- 4) SDP / SAP ----------------------------------------------------------------
DANTE_SDP = ("v=0\r\no=- 1311738121 1311738121 IN IP4 192.168.1.40\r\ns=Dante-FOH-MTX : 2\r\n"
             "c=IN IP4 239.69.1.40/32\r\nt=0 0\r\na=keywds:Dante\r\nm=audio 5004 RTP/AVP 98\r\n"
             "i=2 channels: Left, Right\r\na=recvonly\r\na=rtpmap:98 L24/48000/2\r\na=ptime:1\r\n"
             "a=ts-refclk:ptp=IEEE1588-2008:00-1D-C1-FF-FE-12-34-56:0\r\na=mediaclk:direct=0\r\n")
d = parse_sdp(DANTE_SDP)
check("SDP Dante: name/addr/port/fmt/ch/ptime", d == {"name": "Dante-FOH-MTX : 2", "addr": "239.69.1.40", "port": 5004,
      "fmt": "L24", "sr": 48000, "ch": 2, "ptime": 1.0, "pt": "98"}, str(d))
RAV_SDP = ("v=0\no=- 3 0 IN IP4 10.0.0.5\ns=Ravenna Stagebox 1-8\nc=IN IP4 239.1.2.3/15\nt=0 0\n"
           "m=audio 5004 RTP/AVP 96\na=rtpmap:96 L16/48000/8\na=ptime:0.125\na=sync-time:0\n"
           "a=framecount:6\na=ts-refclk:ptp=IEEE1588-2008:00-0B-72-FF-FE-11-22-33:0\na=mediaclk:direct=0\n")
d = parse_sdp(RAV_SDP)
check("SDP Ravenna: L16/8ch/125µs", d and d["fmt"] == "L16" and d["ch"] == 8 and d["ptime"] == 0.125 and d["addr"] == "239.1.2.3", str(d))
check("SDP: 映像mは None", parse_sdp("v=0\ns=x\nc=IN IP4 239.1.1.1\nm=video 5004 RTP/AVP 96\na=rtpmap:96 H264/90000\n") is None)
check("SDP: rtpmap無しは None", parse_sdp("v=0\ns=x\nc=IN IP4 239.1.1.1\nm=audio 5004 RTP/AVP 96\n") is None)
check("SDP: 空/ゴミは None", parse_sdp("") is None and parse_sdp("hello") is None)
multi = "v=0\ns=x\nc=IN IP4 239.1.1.1\nm=audio 5004 RTP/AVP 97 98\na=rtpmap:97 L16/48000/2\na=rtpmap:98 L24/48000/2\n"
d = parse_sdp(multi)
check("SDP: 複数PTは m= の先頭PTを採用", d and d["fmt"] == "L16", str(d))

sap_pkt = bytes([0x20, 0x00]) + b"\x12\x34" + socket.inet_aton("192.168.1.40") + b"application/sdp\0" + DANTE_SDP.encode()
check("SAP: payload type付き → SDP", parse_sap(sap_pkt) == DANTE_SDP)
sap_noptype = bytes([0x20, 0x01]) + b"\x12\x34" + socket.inet_aton("192.168.1.40") + b"\0\0\0\0" + DANTE_SDP.encode()
check("SAP: 認証データ4B・payload type無し → SDP", parse_sap(sap_noptype) == DANTE_SDP)
check("SAP: 削除(T=1)は None", parse_sap(bytes([0x24, 0]) + b"\0" * 6 + DANTE_SDP.encode()) is None)
check("SAP: version≠1 は None", parse_sap(bytes([0x40, 0]) + b"\0" * 6 + DANTE_SDP.encode()) is None)
import zlib
sap_z = bytes([0x22, 0x00]) + b"\x12\x34" + socket.inet_aton("10.0.0.1") + zlib.compress(b"application/sdp\0" + RAV_SDP.encode())
check("SAP: zlib圧縮(C=1)を解く", parse_sap(sap_z) == RAV_SDP)

# ---- 5) 統合: 合成AES67送信 → source.py --aes67 → server → listener SL2 --------------
def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def sender(port, seconds, amp=0.5, freq=1000.0, drop_every=0):
    """L24 2ch 48k 1ms(48サンプル) の正弦波を RTP で 127.0.0.1:port へ。drop_every>0 なら n個に1個落とす。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = 0; t0 = time.perf_counter(); total = int(seconds * 1000)
    while n < total:
        t = (np.arange(48) + 48 * n) / SR
        v = (np.sin(2 * np.pi * freq * t) * amp * 8388607).astype(np.int64)
        payload = b"".join(int(x).to_bytes(3, "big", signed=True) * 2 for x in v)   # 2ch 同値
        if not (drop_every and n % drop_every == drop_every - 1):
            s.sendto(rtp(n, 48 * n, payload, ssrc=0xABCD), ("127.0.0.1", port))
        n += 1
        target = t0 + n / 1000.0
        d = target - time.perf_counter()
        if d > 0: time.sleep(d)
    s.close()

async def listen_frames(base_ws, ch, seconds):
    import websockets
    frames = []
    async with websockets.connect(f"{base_ws}/audio?role=listen&ch={ch}&pos=L", max_size=None) as ws:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, (bytes, bytearray)):
                frames.append(bytes(m))
    return frames

def run_integration(drop_every, label):
    udp = free_udp_port()
    base_ws = f"ws://127.0.0.1:{PORT}"
    ch = "aes" + label
    log = open(os.path.join(DATA_DIR, f"source-aes67-{label}.log"), "w+b")
    src = subprocess.Popen([sys.executable, os.path.join(ROOT, "source.py"), "--aes67", f"127.0.0.1:{udp}",
                            "--aes67-ch", "2", "--aes67-fmt", "L24", "--server", base_ws, "--ch", ch, "--lead", "0.1"],
                           stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(1.0)                                     # source が接続・受信待ちに入るまで
        th = threading.Thread(target=sender, args=(udp, 3.0, 0.5, 1000.0, drop_every), daemon=True); th.start()
        frames = asyncio.run(listen_frames(base_ws, ch, 3.5))
        th.join(5)
    finally:
        src.terminate()
        try: src.wait(3)
        except subprocess.TimeoutExpired: src.kill()
        log.seek(0); text = log.read().decode(errors="replace"); log.close()
    return frames, text

with ServerProc():
    frames, text = run_integration(0, "clean")
    check("aes67→server: source.py が接続・受信開始", "[aes67] rx 127.0.0.1" in text, text[-300:])
    check("aes67→server: リスナーにSL2フレーム到着(≥100/3s)", len(frames) >= 100, f"n={len(frames)}")
    if frames:
        magic, ver, nchan, _, seq, nsamp, play_at = HEADER.unpack_from(frames[0], 0)
        check("SL2: 1ch(pos L)・960サンプル・playAt付き", magic == b"SL2" and nchan == 1 and nsamp == FRAME and play_at > 0,
              f"nchan={nchan} nsamp={nsamp} play_at={play_at:.3f}")
        pcm = np.concatenate([np.frombuffer(f[HEADER.size:], dtype="<i2") for f in frames[5:-5]]).astype(np.float32) / 32767
        rms = float(np.sqrt(np.mean(pcm ** 2))); exp = 0.5 / np.sqrt(2)
        check("SL2: 1kHz 正弦 amp0.5 の RMS ≈ 0.354 (±8%)", abs(rms - exp) < exp * 0.08, f"rms={rms:.3f}")
        seqs = [HEADER.unpack_from(f, 0)[4] for f in frames]
        check("SL2: seq 連番(サーバ経由でも欠けない)", all(b - a == 1 for a, b in zip(seqs, seqs[1:])), f"first={seqs[0]} last={seqs[-1]} n={len(seqs)}")
        spec = np.abs(np.fft.rfft(pcm[:SR])); peak_hz = float(np.argmax(spec) * SR / len(pcm[:SR]))
        check("SL2: スペクトルピーク 1kHz(±10Hz)", abs(peak_hz - 1000) < 10, f"peak={peak_hz:.0f}Hz")

    frames, text = run_integration(10, "gap")             # 10個に1個(48サンプル/10ms毎)落とす
    check("aes67 欠落10%: 位相は保たれフレームは連続", len(frames) >= 100, f"n={len(frames)}")
    if frames:
        pcm = np.concatenate([np.frombuffer(f[HEADER.size:], dtype="<i2") for f in frames[5:-5]]).astype(np.float32) / 32767
        rms = float(np.sqrt(np.mean(pcm ** 2))); exp = 0.5 / np.sqrt(2) * np.sqrt(0.9)
        check("aes67 欠落10%: 無音で埋まりRMSが√0.9倍(±10%)", abs(rms - exp) < exp * 0.10, f"rms={rms:.3f} exp={exp:.3f}")
        # 欠落を無音で埋めた=サンプル数は保たれる → 3秒で約150フレーム(±15%)
        check("aes67 欠落10%: サンプル数は欠けない(≈150フレーム/3s)", 120 <= len(frames) <= 175, f"n={len(frames)}")

# ---- 6) multicast join(環境依存: 失敗しても FAIL にしない・報告のみ) -----------------
try:
    s = source.open_udp("239.69.83.83", free_udp_port()); s.close()
    check("multicast join 239.69.x.x (この環境)", True)
except OSError as e:
    print(f"  ⚠ multicast join skipped on this runner: {e}")

print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
if ng:
    print("failed:", ng)
sys.exit(1 if ng else 0)
