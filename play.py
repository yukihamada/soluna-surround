#!/usr/bin/env python3
"""
SOLUNA Sound — speaker node (Raspberry Pi / any machine with an audio out).
No browser, no tap. Runs the whole show protocol like a phone would:

  * LIVE  — SL2 PCM frames scheduled at server `playAt` (+ zone delay)      [v2]
  * CUE   — pre-distributed track (mp3/wav/…) decoded once, scheduled at `at`
            on the same clock; mid-join lands inside the track in phase       [v6]
  * PRELOAD / cue_stop / zone walk-test (`zones:[..]`) / SHOW steps           [v6]
  * LIGHT — pattern+phase received; forwarded to an optional hook command
            (`--light-cmd`), e.g. a GPIO/WS281x driver script               [v6]
  * REPORT — tells FOH what this node is really doing (playing/preloaded…)   [v6]
  * reconnects forever (field WiFi drops must not kill a speaker)            [v6]

  python3 play.py L --server ws://192.168.1.10:8900 --ch festival --zone B

Decoding: ffmpeg if present (any format) → soundfile (wav/flac/ogg) → stdlib wave.
Clock: median of the 3 lowest-RTT samples in a 30-sample window (same as client).
"""
import argparse, asyncio, json, os, shutil, socket, struct, subprocess, sys, threading, time, wave, io
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import discover                       # --server auto(ゼロコンフィグ: サーバを自動発見)
except Exception:
    discover = None
NODE_JSON = os.environ.get("SOLUNA_NODE_JSON", "/opt/soluna/node.json")

import numpy as np
try:
    import sounddevice as sd
except Exception:            # CI / headless: 音デバイス無しでもプロトコル部分は使える
    sd = None
try:
    import websockets
except Exception:
    websockets = None

SR = 48000
HEADER = struct.Struct("<3sBBBIId")
RING_SEC = 4
RING = RING_SEC * SR
PAN = {"L": (1.0, 0.0), "C": (0.7071, 0.7071), "R": (0.0, 1.0)}
VERSION = "v6"


# ---- decode -----------------------------------------------------------------
def decode_to_f32_mono(data: bytes, sr: int = SR) -> np.ndarray:
    """任意フォーマット → float32 mono @sr。ffmpeg → soundfile → wave の順に試す。"""
    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le",
                            "-ac", "1", "-ar", str(sr), "pipe:1"],
                           input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 and p.stdout:
            return np.frombuffer(p.stdout, dtype="<f4").copy()
    try:
        import soundfile as sf
        y, fs = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        y = y.mean(axis=1)
        return _resample(y, fs, sr)
    except Exception:
        pass
    with wave.open(io.BytesIO(data)) as w:
        n, ch, fs, sw = w.getnframes(), w.getnchannels(), w.getframerate(), w.getsampwidth()
        raw = w.readframes(n)
        if sw == 2:
            y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif sw == 4:
            y = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        elif sw == 1:
            y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"unsupported wav sampwidth {sw}")
        y = y.reshape(-1, ch).mean(axis=1)
        return _resample(y, fs, sr)


def _resample(y: np.ndarray, fs: int, sr: int) -> np.ndarray:
    if fs == sr:
        return y.astype(np.float32)
    n_out = int(round(len(y) * sr / fs))
    x_old = np.linspace(0.0, 1.0, num=len(y), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, y).astype(np.float32)


class Player:
    def __init__(self, pos, zone=None, light_cmd=None):
        self.pos = pos
        self.zone = (zone or "").upper() or None
        self.zones = {}            # サーバconfig: zone -> delay_ms
        self.zone_gain_db = {}     # サーバconfig: zone -> dB
        self.base_ms = 0.0         # ハウスPA位相合わせトリム
        self.gain_db = 0.0         # このノード固有の音量補正(--gain-db)
        self.muted = False         # {"t":"mute","on":true} で全ノード即時無音
        self.tone_left = 0         # {"t":"tone"} セットアップ画面の🔔テスト音(残りサンプル数)
        self.tone_hz = 880.0
        self.tone_phase = 0
        self.asset_base = None
        self.gl, self.gr = PAN.get(pos.upper(), (0.7071, 0.7071))
        self.ring = np.zeros((RING, 2), dtype=np.float32)
        self.lock = threading.Lock()
        self.t0_stream = None      # stream time (s) at first callback
        self.t0_wall = None        # wall clock (s) at first callback
        self.epoch_off = None      # server_epoch = local_wall + epoch_off
        self.sync = []             # [(rtt_ms, off_s)] window
        self.best_rtt_ms = None
        self.stats = {"frames": 0, "late": 0, "under": 0}
        # CUE: 曲全体を持ち、callbackでストリームサンプル位置から直接読む
        self.cue = None            # {"id","buf","start","loop","gain"}  start=stream sample of sample0
        self.cue_msg = None        # 最後に受けた cue(再同期用)
        self.cache = {}            # url -> f32 buffer (PRELOAD)
        self.state = "idle"        # idle|preloaded|playing|failed
        self.light = None
        self.light_cmd = light_cmd
        self.http_base = None
        self.host = socket.gethostname()
        self.node_json = NODE_JSON

    # ---- 割当(ゼロコンフィグ: /admin NODES から zone/pos/gain を押し込まれる) ----
    def apply_assign(self, m):
        """{"t":"assign","zone":"C","pos":"L","gain_db":-3} → 即時反映 + node.json に永続。
        次回起動は node.json を読む(CLIで明示された値だけが上書きする)。"""
        changed = False
        if m.get("zone") is not None:
            z = str(m["zone"]).upper() or None
            if z != self.zone:
                self.zone, changed = z, True
        if m.get("pos") is not None:
            pos = str(m["pos"]).upper()[:1]
            if pos in PAN and pos != self.pos:
                self.pos = pos
                self.gl, self.gr = PAN[pos]
                changed = True
        if m.get("gain_db") is not None:
            g = float(m["gain_db"])
            if g != self.gain_db:
                self.gain_db, changed = g, True
        if changed:
            self.rearm()
            self.save_node_json()
        return changed

    def node_cfg(self):
        return {"zone": self.zone, "pos": self.pos, "gain_db": self.gain_db}

    def save_node_json(self, path=None):
        path = path or self.node_json
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path + ".tmp", "w") as f:
                json.dump(self.node_cfg(), f)
            os.replace(path + ".tmp", path)
            return True
        except Exception as e:
            print(f"[play {self.pos}] node.json not saved ({path}): {e}")
            return False

    @staticmethod
    def load_node_json(path=None):
        try:
            with open(path or NODE_JSON) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

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
            cue = self.cue
        if cue is not None:
            block += self.cue_block(cue, start, nframes)
        if self.muted:                     # FOHキルスイッチ: 位相を保ったまま無音(解除でそのまま復帰)
            block[:] = 0.0
        if self.tone_left > 0:             # テスト音: -12dBFS サイン(ミュート中でも鳴る=配線確認用)
            n = min(nframes, self.tone_left)
            ph = self.tone_phase + np.arange(n)
            block[:n] += (0.25 * np.sin(2 * np.pi * self.tone_hz * ph / SR)).astype(np.float32)[:, None]
            self.tone_phase += n
            self.tone_left -= n
        outdata[:] = np.clip(block, -1.0, 1.0)

    def cue_block(self, cue, start, nframes):
        """ストリームサンプル [start, start+nframes) に対応するCUE音を返す(pan込み)。"""
        buf = cue["buf"]
        n = len(buf)
        pos = np.arange(start, start + nframes) - cue["start"]
        out = np.zeros((nframes, 2), dtype=np.float32)
        if n == 0:
            return out
        if cue["loop"]:
            pos = pos % n
            valid = np.ones(nframes, dtype=bool)
        else:
            valid = (pos >= 0) & (pos < n)
            if not valid.any():
                if pos[0] >= n and self.state == "playing":
                    self.state = "idle"    # 曲が終わった
                return out
            pos = np.clip(pos, 0, n - 1)
        mono = buf[pos] * cue["gain"] * self.level_gain()
        mono[~valid] = 0.0
        out[:, 0] = mono * self.gl
        out[:, 1] = mono * self.gr
        return out

    # ---- wall<->stream mapping ----
    def stream_sample_for_wall(self, wall):
        return int(round((self.t0_stream + (wall - self.t0_wall)) * SR))

    def delay_sec(self):
        z = self.zones.get(self.zone, 0.0) if self.zone else 0.0
        return max(0.0, (z + self.base_ms) / 1000.0)

    def level_gain(self):
        zg = self.zone_gain_db.get(self.zone, 0.0) if self.zone else 0.0
        return float(10 ** ((self.gain_db + zg) / 20.0))

    def on_pong(self, c_ms, s_ms):
        now = time.time() * 1000
        rtt = now - c_ms
        off = (s_ms + rtt / 2 - now) / 1000.0
        self.sync.append((rtt, off))
        if len(self.sync) > 30:
            self.sync.pop(0)
        low3 = sorted(self.sync)[:3]
        offs = sorted(o for _, o in low3)
        self.epoch_off = offs[len(offs) // 2]
        self.best_rtt_ms = low3[0][0]

    # ---- LIVE ----
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
        g = self.level_gain()
        with self.lock:
            self.ring[idx, 0] += mono_f32 * self.gl * g
            self.ring[idx, 1] += mono_f32 * self.gr * g
        self.stats["frames"] += 1

    # ---- CUE ----
    def resolve_url(self, url):
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if self.asset_base and url.startswith("/assets/"):
            return self.asset_base + "/" + url[len("/assets/"):]
        return (self.http_base or "") + url

    def fetch_track(self, url):
        if url in self.cache:
            return self.cache[url]
        # CDN(asset_base)が先、失敗したらサーバ直(R2未同期の曲でもショーは止まらない)
        candidates = [self.resolve_url(url)]
        direct = (self.http_base or "") + url if url.startswith("/") else url
        if direct not in candidates:
            candidates.append(direct)
        data, last = None, None
        for full in candidates:
            try:
                with urlopen(Request(full, headers={"User-Agent": f"soluna-node/{VERSION}"}), timeout=60) as r:
                    data = r.read()
                break
            except Exception as e:
                last = e
                print(f"[play {self.pos}] fetch failed {full}: {e}")
        if data is None:
            raise last or RuntimeError("fetch failed")
        buf = decode_to_f32_mono(data, SR)
        self.cache[url] = buf
        return buf

    def arm_cue(self, m):
        """cue メッセージ → ストリーム上の開始サンプルを計算(途中参加なら過去に置く=曲中位置)。"""
        if m.get("zones") and (self.zone not in [z.upper() for z in m["zones"]]):
            return False                                   # ウォークテスト: 他ゾーン宛
        url = m.get("url")
        if not url:                                        # 映像だけのcue: ノードは無関係
            return False
        if self.epoch_off is None or self.t0_stream is None:
            self.cue_msg = m                               # 同期後に再試行
            return False
        buf = self.fetch_track(url)
        target_wall = float(m["at"]) + self.delay_sec() - self.epoch_off
        start = self.stream_sample_for_wall(target_wall)
        with self.lock:
            self.cue = {"id": m.get("id"), "buf": buf, "start": start,
                        "loop": bool(m.get("loop")), "gain": float(m.get("gain", 1.0))}
        self.cue_msg = m
        self.state = "playing"
        return True

    def stop_cue(self):
        with self.lock:
            self.cue = None
        self.cue_msg = None
        if self.state == "playing":
            self.state = "idle"

    def rearm(self):
        """ゾーン/遅延/時計が変わった → 同じcueを新しい位置で置き直す(曲中同期)。"""
        if self.cue_msg:
            try:
                self.arm_cue(self.cue_msg)
            except Exception as e:
                print(f"[play {self.pos}] rearm failed: {e}")

    # ---- LIGHT (hook) ----
    def on_light(self, m):
        self.light = m
        if self.light_cmd:
            try:
                subprocess.Popen([self.light_cmd, json.dumps(m)])
            except Exception as e:
                print(f"[play {self.pos}] light-cmd failed: {e}")

    def report(self):
        return {"t": "report", "st": self.state, "ctx": "running",
                "acc": None if self.best_rtt_ms is None else self.best_rtt_ms / 2,
                "cue": (self.cue or {}).get("id") or "", "kind": "node", "host": self.host}


async def session(player, server, ch, node_id):
    url = f"{server}/audio?role=listen&ch={ch}&pos={player.pos}&host={player.host}"
    if player.zone:
        url += f"&zone={player.zone}"
    u = urlparse(server)
    player.http_base = f"{'https' if u.scheme == 'wss' else 'http'}://{u.netloc}"
    async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
        player.sync.clear()

        async def pinger():
            n = 0
            while True:
                await ws.send(json.dumps({"t": "ping", "c": time.time() * 1000}))
                n += 1
                if n % 10 == 0:
                    await ws.send(json.dumps(player.report()))
                await asyncio.sleep(0.5 if n < 10 else 3.0)
        ping_task = asyncio.create_task(pinger())
        loop = asyncio.get_running_loop()
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    m = json.loads(msg)
                    t = m.get("t")
                    if t == "config":
                        player.zones = {k.upper(): float(v)
                                        for k, v in (m.get("zones") or {}).items()}
                        player.zone_gain_db = {k.upper(): float(v)
                                               for k, v in (m.get("zone_gain_db") or {}).items()}
                        player.base_ms = float(m.get("base_ms", 0.0))
                        player.asset_base = m.get("asset_base") or None
                        if int(m.get("sr", SR)) != SR:
                            print(f"[play {player.pos}] ⚠ source sr={m['sr']} != {SR}: "
                                  f"LIVEノード運用は48kHz送出(source.py)が前提。ピッチが狂います")
                        print(f"[play {player.pos}] config zone={player.zone} "
                              f"delay={player.delay_sec()*1000:.1f}ms gain={player.level_gain():.2f}")
                        await loop.run_in_executor(None, player.rearm)
                    elif t == "pong":
                        first = player.epoch_off is None
                        player.on_pong(m["c"], m["s"])
                        if first and player.cue_msg:
                            await loop.run_in_executor(None, player.rearm)
                    elif t == "preload":
                        def _pre():
                            try:
                                player.fetch_track(m["url"])
                                if player.state != "playing":
                                    player.state = "preloaded"
                            except Exception as e:
                                player.state = "failed"
                                print(f"[play {player.pos}] preload failed: {e}")
                        await loop.run_in_executor(None, _pre)
                        await ws.send(json.dumps(player.report()))
                    elif t == "cue":
                        def _cue():
                            try:
                                if player.arm_cue(m):
                                    print(f"[play {player.pos}] CUE {m.get('id')} at={m['at']:.3f}")
                            except Exception as e:
                                player.state = "failed"
                                print(f"[play {player.pos}] cue failed: {e}")
                        await loop.run_in_executor(None, _cue)
                        await ws.send(json.dumps(player.report()))
                    elif t == "tone":
                        player.tone_hz = float(m.get("hz") or 880.0)
                        player.tone_phase = 0
                        player.tone_left = int(SR * max(0.1, min(5.0, float(m.get("sec") or 0.6))))
                        print(f"[play {player.pos}] TONE {player.tone_hz:.0f}Hz")
                    elif t == "mute":
                        player.muted = bool(m.get("on"))
                        print(f"[play {player.pos}] MUTE {'on' if player.muted else 'off'}")
                    elif t == "cue_stop":
                        player.stop_cue()
                        await ws.send(json.dumps(player.report()))
                    elif t == "light":
                        player.on_light(m)
                    elif t == "light_stop":
                        player.on_light({"pattern": "off"})
                    elif t == "assign":
                        if player.apply_assign(m):
                            print(f"[play {player.pos}] ASSIGN zone={player.zone} pos={player.pos} gain={player.gain_db}dB")
                            await ws.send(json.dumps({"t": "zone", "zone": player.zone or ""}))
                            await ws.send(json.dumps({"t": "pos", "pos": player.pos}))
                            await ws.send(json.dumps(player.report()))
                else:
                    if len(msg) <= HEADER.size:
                        continue
                    _, _, nchan, _p, seq, nsamp, play_at = HEADER.unpack_from(msg, 0)
                    pcm = np.frombuffer(msg, dtype="<i2", count=nsamp, offset=HEADER.size)
                    player.schedule(pcm.astype(np.float32) / 32768.0, play_at)
        finally:
            ping_task.cancel()


async def net(player, server, ch, node_id):
    """切れても必ず戻る: 現場WiFiの瞬断でスピーカーが黙ったままにならない。
    再接続後は config/cue が再送されるので、進行中の曲へ曲中復帰する。"""
    backoff = 1.0
    while True:
        try:
            print(f"[play {player.pos}] connecting {server} ch={ch}")
            await session(player, server, ch, node_id)
            backoff = 1.0
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            print(f"[play {player.pos}] disconnected: {e!r} — retry in {backoff:.0f}s")
        player.epoch_off = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15.0)


def pick_device(spec):
    """--device auto: 外付けDACを優先、無ければ既定。
    優先順: USB音源 > GPIO I2S DAC(HiFiBerry/PCM5102/MAX98357等) > 内蔵(bcm2835 3.5mm/HDMI)。
    Piの内蔵3.5mmはノイズが多いので、外付けが1つでも見えれば必ずそちらを使う。"""
    if spec is None or spec == "" or spec == "default":
        return None
    if spec != "auto":
        try:
            return int(spec)
        except ValueError:
            return spec
    BUILTIN = ("bcm2835", "vc4", "hdmi", "sysdefault", "default", "dmix", "pulse", "pipewire")
    try:
        devs = [(i, d) for i, d in enumerate(sd.query_devices()) if d.get("max_output_channels", 0) >= 1]
        for i, d in devs:
            if "usb" in d.get("name", "").lower():
                print(f"[play] output → USB audio: [{i}] {d['name']}")
                return i
        for i, d in devs:
            n = d.get("name", "").lower()
            if n and not any(b in n for b in BUILTIN):
                print(f"[play] output → external DAC: [{i}] {d['name']}")
                return i
    except Exception as e:
        print(f"[play] device scan failed: {e}")
    print("[play] output → system default (no USB/I2S DAC found)")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pos", nargs="?", choices=["L", "C", "R", "l", "c", "r"],
                    help="L|C|R(省略時: node.json の割当 → C)")
    ap.add_argument("--server", default="auto",
                    help="ws(s)://host:port。auto=/etc/soluna/node.env の SERVER、無ければ LAN 上のサーバを自動発見")
    ap.add_argument("--ch", default="festival")
    ap.add_argument("--zone", help="ゾーン名(A..F等)。サーバのzone表の遅延を適用")
    ap.add_argument("--gain-db", type=float, default=0.0, help="このノードの音量補正[dB]")
    ap.add_argument("--light-cmd", help="ライト受信時に実行するコマンド(引数=JSON)。GPIO/WS281xドライバ等")
    ap.add_argument("--node-id", default=os.uname().nodename if hasattr(os, "uname") else "node")
    ap.add_argument("--device", default="auto",
                    help="sounddevice 出力デバイス名/番号。auto=USB音源があればそれ、無ければ既定")
    a = ap.parse_args()
    if sd is None or websockets is None:
        sys.exit("pip install sounddevice websockets numpy  (and ffmpeg for mp3)")
    saved = Player.load_node_json()                      # /admin からの割当(前回)。CLI明示が優先
    pos = (a.pos or saved.get("pos") or "C").upper()
    zone = a.zone if a.zone is not None else saved.get("zone")
    player = Player(pos, zone=zone, light_cmd=a.light_cmd)
    player.gain_db = a.gain_db if a.gain_db != 0.0 else float(saved.get("gain_db") or 0.0)
    if saved:
        print(f"[play {pos}] node.json: zone={zone} pos={pos} gain={player.gain_db}dB")
    server = a.server
    if server == "auto":
        if discover is None:
            sys.exit("--server auto needs discover.py next to play.py")
        server = discover.resolve_server("auto", wait=True, log=print)

    dev = pick_device(a.device)
    stream = sd.OutputStream(samplerate=SR, channels=2, dtype="float32",
                             blocksize=480, callback=player.callback, device=dev)
    stream.start()
    print(f"[play {player.pos}] speaker live device={dev if dev is not None else 'default'} (pan L={player.gl} R={player.gr}) "
          f"ffmpeg={'yes' if shutil.which('ffmpeg') else 'no (wav only)'}")

    def status_loop():
        while True:
            time.sleep(2)
            s = player.stats
            print(f"[play {player.pos}] {player.state} frames={s['frames']} late={s['late']} "
                  f"cue={(player.cue or {}).get('id') or '—'} "
                  f"sync={'±%.1fms' % (player.best_rtt_ms/2) if player.best_rtt_ms else '—'}")
    threading.Thread(target=status_loop, daemon=True).start()

    try:
        asyncio.run(net(player, server, a.ch, a.node_id))
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop(); stream.close()


if __name__ == "__main__":
    main()
