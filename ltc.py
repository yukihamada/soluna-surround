#!/usr/bin/env python3
"""
SOLUNA Sound — SMPTE 12M LTC(Linear Timecode)の復号/生成。numpy のみ。

  LTCDecoder.feed(block: float32 mono) -> [Frame, ...]
      Frame = dict(hh, mm, ss, ff, fps, drop, start, end)
      start/end = そのフレームの先頭/末尾のサンプル番号(feed 累積の絶対位置)。
      fps は DFフラグと実測フレーム長(2000/1920/1600/1601.6 samples@48k)から推定。
  ltc_encode(...) / ltc_stream(...) — テストと信号発生用(バイフェーズマーク 80bit)。

CLI: 再生卓から出ている LTC をオーディオ入力で受け、復号できたら SOLUNA サーバへ
「今この瞬間の番組TC」として POST /api/timecode する(毎秒1回)。
    python3 ltc.py --device 3 --server http://192.168.1.10:8900 --token $SOLUNA_ADMIN
    python3 ltc.py --wav ltc.wav                     # ファイルを復号して表示だけ
    python3 ltc.py --list                            # 入力デバイス一覧
"""
import argparse
import json
import sys
import time
import urllib.request

import numpy as np

SR = 48000
SYNC_WORD = (0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1)      # bits 64..79 (forward)
_FRAME_LEN = {24: SR / 24.0, 25: SR / 25.0, 30: SR / 30.0, 29.97: SR * 1001 / 30000.0}


def _bcd_bits(value, nbits):
    return [(value >> i) & 1 for i in range(nbits)]


def ltc_frame_bits(hh, mm, ss, ff, drop=False, fps=30):
    """80bit のフレーム(bit0 が先に送られる)。ユーザービットは 0。
    パリティ(バイフェーズマーク極性補正)= 25fps は bit27、他は bit59: 80bit中の0の数を偶数に。"""
    b = [0] * 80
    b[0:4] = _bcd_bits(ff % 10, 4)
    b[8:10] = _bcd_bits(ff // 10, 2)
    b[10] = 1 if drop else 0
    b[16:20] = _bcd_bits(ss % 10, 4)
    b[24:27] = _bcd_bits(ss // 10, 3)
    b[32:36] = _bcd_bits(mm % 10, 4)
    b[40:43] = _bcd_bits(mm // 10, 3)
    b[48:52] = _bcd_bits(hh % 10, 4)
    b[56:58] = _bcd_bits(hh // 10, 2)
    b[64:80] = list(SYNC_WORD)
    pbit = 27 if int(round(fps)) == 25 else 59
    if b.count(0) % 2 == 1:
        b[pbit] = 1
    return b


def decode_bits(b):
    """80bit → (hh, mm, ss, ff, drop)。sync は呼び出し側で確認済み前提。"""
    def val(lo, n):
        return sum(b[lo + i] << i for i in range(n))
    ff = val(0, 4) + 10 * val(8, 2)
    ss = val(16, 4) + 10 * val(24, 3)
    mm = val(32, 4) + 10 * val(40, 3)
    hh = val(48, 4) + 10 * val(56, 2)
    return hh, mm, ss, ff, bool(b[10])


class _Phase:
    """非整数 samples/bit(29.97 → 20.02)を累積誤差なしで刻む。"""

    def __init__(self, level=0.5, polarity=1.0):
        self.level = level
        self.pol = polarity
        self.acc = 0.0                      # 端数キャリー

    def emit(self, n_float):
        n = int(np.floor(n_float + self.acc))
        self.acc = n_float + self.acc - n
        return np.full(n, self.level * self.pol, dtype=np.float32)


def ltc_encode(hh, mm, ss, ff, fps=30, drop=False, sr=SR, level=0.5, phase=None):
    """1フレームぶんのバイフェーズマーク波形。phase を渡すと極性/端数が次フレームへ継続。"""
    eff = (30000.0 / 1001.0) if (drop and int(round(fps)) == 30) else float(fps)
    rate = sr / eff                         # samples per frame(29.97 → 1601.6)
    spb = rate / 80.0                       # samples per bit
    ph = phase or _Phase(level)
    bits = ltc_frame_bits(hh, mm, ss, ff, drop, fps)
    out = []
    for bit in bits:
        ph.pol = -ph.pol                    # ビット境界で必ず遷移
        if bit:
            out.append(ph.emit(spb / 2))
            ph.pol = -ph.pol                # "1" はビット中央でもう1回遷移
            out.append(ph.emit(spb / 2))
        else:
            out.append(ph.emit(spb))
    return np.concatenate(out), ph


def ltc_stream(start_tc, fps=30, drop=False, n_frames=10, sr=SR, level=0.5):
    """start_tc='HH:MM:SS:FF' から n_frames 連続。(waveform, [tc strings])"""
    hh, mm, ss, ff = (int(x) for x in start_tc.replace(";", ":").split(":"))
    nominal = int(round(fps))
    ph = _Phase(level)
    chunks, tcs = [], []
    for _ in range(n_frames):
        w, ph = ltc_encode(hh, mm, ss, ff, fps, drop, sr, level, ph)
        chunks.append(w)
        tcs.append(f"{hh:02d}:{mm:02d}:{ss:02d}{';' if drop else ':'}{ff:02d}")
        ff += 1
        if ff >= nominal:
            ff = 0; ss += 1
            if ss >= 60:
                ss = 0; mm += 1
                if mm >= 60:
                    mm = 0; hh = (hh + 1) % 24
                if drop and mm % 10 != 0:
                    ff = 2                  # DF: 毎分頭の ;00 ;01 を飛ばす(10分毎は除く)
    return np.concatenate(chunks), tcs


class LTCDecoder:
    """ストリーミング復号。任意長のブロックを feed し、フレーム境界を跨いでも復号する。"""

    def __init__(self, sr=SR):
        self.sr = sr
        self.pos = 0                        # 累積サンプル数(次ブロック先頭の絶対位置)
        self.last_sign = 0
        self.last_edge = None               # 直前の遷移位置(小数サンプル)
        self.T = None                       # 1ビット長[samples]の推定
        self.warm = []                      # T 推定用の初期インターバル
        self.pending_half = None            # "1" の前半を見た位置
        self.bits = []                      # 直近ビット列(最大80+)
        self.bit_edges = []                 # 各ビット先頭の遷移位置
        self.last_frame_end = None
        self.frames_seen = 0
        self.peak = 0.1
        self.dc = 0.0
        self.last_val = 0.0

    def _classify(self, d, edge):
        """遷移間隔 d → ビット列へ。"""
        if self.T is None:
            self.warm.append(d)
            if len(self.warm) >= 48:
                # 大きい方の塊 = 1ビット長。連続する "1"(半ビット)だけの区間に当たっても
                # 48 遷移の中には必ず "0"(フルビット)が含まれる(sync前後に 0 がある)。
                w = sorted(self.warm)
                self.T = float(np.median([v for v in w if v > 0.75 * w[-1]]))
                self.warm = []
            return
        if d > 0.75 * self.T:                 # フルビット = "0"
            if d < 1.4 * self.T:
                self.T = 0.9 * self.T + 0.1 * d
            self.pending_half = None
            self._push(0, edge - d)
        else:                                 # 半ビット
            if self.pending_half is None:
                self.pending_half = edge - d
            else:
                self.T = 0.9 * self.T + 0.1 * (edge - self.pending_half)
                self._push(1, self.pending_half)
                self.pending_half = None

    def _push(self, bit, start_edge):
        self.bits.append(bit)
        self.bit_edges.append(start_edge)
        if len(self.bits) > 96:
            del self.bits[:-96]
            del self.bit_edges[:-96]
        if len(self.bits) >= 80 and tuple(self.bits[-16:]) == SYNC_WORD:
            frame_bits = self.bits[-80:]
            start = self.bit_edges[-80]
            end = start + 80 * self.T
            hh, mm, ss, ff, drop = decode_bits(frame_bits)
            if hh < 24 and mm < 60 and ss < 60 and ff < 60:
                self._emit(hh, mm, ss, ff, drop, start, end)
            self.bits = []
            self.bit_edges = []

    def _emit(self, hh, mm, ss, ff, drop, start, end):
        frame_len = 80 * self.T
        if drop:
            fps = 29.97
        else:
            fps = min(_FRAME_LEN.keys(), key=lambda k: abs(_FRAME_LEN[k] * self.sr / SR - frame_len))
            if fps == 29.97:
                fps = 30                      # DFフラグ無しの 29.97 は事実上 30 として扱う
        self.out.append({"hh": hh, "mm": mm, "ss": ss, "ff": ff, "fps": fps, "drop": drop,
                         "start": int(round(start)), "end": int(round(end)),
                         "tc": f"{hh:02d}:{mm:02d}:{ss:02d}{';' if drop else ':'}{ff:02d}"})
        self.frames_seen += 1

    def feed(self, block):
        """block: float32 mono。復号できたフレームのリストを返す。"""
        self.out = []
        x = np.asarray(block, dtype=np.float32)
        if x.size == 0:
            return self.out
        # DC はブロック平均でなく遅い追従で除く(短いブロックの平均は矩形波の位相で暴れる)
        self.dc = 0.95 * self.dc + 0.05 * float(np.mean(x))
        x = x - self.dc
        pk = float(np.max(np.abs(x)))
        self.peak = max(0.9 * self.peak, pk, 1e-3)
        th = 0.15 * self.peak                 # ヒステリシス
        sign = np.where(x > th, 1, np.where(x < -th, -1, 0))
        # 0(不感帯)は直前の符号を引き継ぐ(ブロック先頭は前ブロック末尾の符号から)
        signs = np.empty(x.size + 1, dtype=np.int8)
        signs[0] = self.last_sign
        s = self.last_sign
        for i in range(sign.size):
            if sign[i] != 0:
                s = sign[i]
            signs[i + 1] = s
        xa = np.concatenate(([self.last_val], x))            # xa[k] ↔ signs[k]
        idx = np.nonzero(signs[1:] != signs[:-1])[0] + 1      # 遷移 xa[k-1]→xa[k]
        for k in idx:
            if signs[k - 1] == 0:                               # 起動直後の初期符号は遷移でない
                continue
            a, b = float(xa[k - 1]), float(xa[k])
            frac = a / (a - b) if (a - b) != 0 else 0.0
            frac = min(1.0, max(0.0, frac))
            edge = self.pos - 1 + (k - 1) + frac               # 絶対位置(小数サンプル)
            if self.last_edge is not None:
                d = edge - self.last_edge
                if d > 0.05 * (self.T or 10):
                    self._classify(d, edge)
            self.last_edge = edge
        self.last_sign = int(signs[-1])
        self.last_val = float(x[-1])
        self.pos += x.size
        return self.out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _post_tc(server, token, ch, tc, fps):
    body = json.dumps({"tc": tc, "fps": fps}).encode()
    req = urllib.request.Request(f"{server.rstrip('/')}/api/timecode?ch={ch}", data=body,
                                 headers={"content-type": "application/json",
                                          "x-soluna-admin": token}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser(description="LTC → SOLUNA /api/timecode")
    ap.add_argument("--device", help="sounddevice 入力デバイス(番号/名前)")
    ap.add_argument("--channel", type=int, default=0, help="入力チャンネル(0始まり)")
    ap.add_argument("--wav", help="ファイル復号のみ")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--server", default="http://127.0.0.1:8900")
    ap.add_argument("--token", default="", help="SOLUNA_ADMIN")
    ap.add_argument("--ch", default="festival")
    ap.add_argument("--every", type=float, default=1.0, help="POST 間隔[s]")
    a = ap.parse_args()

    if a.wav:
        import wave
        with wave.open(a.wav, "rb") as w:
            sr = w.getframerate(); n = w.getnchannels()
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        mono = raw[a.channel::n] if n > 1 else raw
        dec = LTCDecoder(sr)
        for i in range(0, mono.size, 960):
            for f in dec.feed(mono[i:i + 960]):
                print(f"{f['tc']}  fps={f['fps']}  @sample {f['start']}")
        print(f"frames decoded: {dec.frames_seen}")
        return

    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("pip install sounddevice  (or use --wav)")
    if a.list:
        print(sd.query_devices()); return
    dev = a.device
    if dev is not None:
        try:
            dev = int(dev)
        except ValueError:
            pass
    dec = LTCDecoder(SR)
    last_post = 0.0
    state = {"tc": None}

    def cb(indata, frames, t_info, status):
        nonlocal last_post
        for f in dec.feed(indata[:, a.channel]):
            state["tc"] = (f["tc"], f["fps"])
        if state["tc"] and time.time() - last_post >= a.every and a.token:
            tc, fps = state["tc"]
            try:
                _post_tc(a.server, a.token, a.ch, tc, fps)
                print(f"[ltc] {tc} fps={fps} → {a.server}")
            except Exception as e:
                print(f"[ltc] post failed: {e}")
            last_post = time.time()

    with sd.InputStream(device=dev, samplerate=SR, channels=a.channel + 1, dtype="float32",
                        blocksize=960, callback=cb):
        print(f"[ltc] listening device={dev} ch={a.channel} → {a.server} (Ctrl-C to stop)")
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
