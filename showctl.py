#!/usr/bin/env python3
"""
SOLUNA Sound — show-control bridges (依存ゼロ: stdlib + asyncio)。

既存フェスの制御系にそのまま挿すための3本:

  * OSC in      QLab / Ableton / grandMA / Eos 等から UDP で SOLUNA のキュー・ライト・
                ショー進行・タイムコードを叩く。/soluna/... アドレス空間(下記 OSC_ADDRESSES)。
  * Timecode    「今この瞬間の番組タイムコードは HH:MM:SS:FF」を1回教えると、以後の
                cue は {"tc":"01:00:10:00"} で発火時刻を指定できる(24/25/30/29.97DF)。
                LTC 音声の復号は ltc.py(こちらは numpy)。
  * DMX out     進行中のライトパターンを 40Hz で DMX に落とし Art-Net / sACN(E1.31) で
                会場の灯体へ送る。色の計算は client.html のライトエンジンと同じ式
                (fixture index = 会場横断位置 x)。

サーバ(server.py)からは以下だけを使う:
    parse_osc_packet(data) -> [(address, args, at_epoch_or_None), ...]
    OscServer(dispatch, port)         asyncio Datagram 受け口
    tc_to_frames / frames_to_seconds / frames_to_tc  タイムコード演算
    DmxOut(get_light, artnet=..., sacn=...)          40Hz 送出タスク
"""
import asyncio
import math
import socket
import struct
import time

# ---------------------------------------------------------------------------
# OSC 1.0 (parse only — we are a receiver)
# ---------------------------------------------------------------------------
_NTP_EPOCH_OFFSET = 2208988800          # 1900-01-01 → 1970-01-01 [s]


def _osc_str(data, i):
    end = data.index(b"\0", i)
    s = data[i:end].decode("utf-8", "replace")
    i = (end + 4) & ~3                  # 終端NUL込みで4バイト境界へ
    return s, i


def parse_osc_message(data: bytes):
    """1メッセージ → (address, [args])。型: i f s b T F N I d h t(→float秒) 。"""
    addr, i = _osc_str(data, 0)
    args = []
    if i >= len(data) or data[i:i + 1] != b",":
        return addr, args
    tags, i = _osc_str(data, i)
    for tag in tags[1:]:
        if tag == "i":
            args.append(struct.unpack_from(">i", data, i)[0]); i += 4
        elif tag == "f":
            args.append(struct.unpack_from(">f", data, i)[0]); i += 4
        elif tag == "d":
            args.append(struct.unpack_from(">d", data, i)[0]); i += 8
        elif tag == "h":
            args.append(struct.unpack_from(">q", data, i)[0]); i += 8
        elif tag == "s" or tag == "S":
            s, i = _osc_str(data, i); args.append(s)
        elif tag == "b":
            n = struct.unpack_from(">i", data, i)[0]; i += 4
            args.append(bytes(data[i:i + n])); i += (n + 3) & ~3
        elif tag == "t":
            sec, frac = struct.unpack_from(">II", data, i); i += 8
            args.append(sec - _NTP_EPOCH_OFFSET + frac / 2 ** 32)
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        elif tag == "N":
            args.append(None)
        elif tag == "I":
            args.append(math.inf)
        else:                            # 未知の型タグ: 以降は解釈不能なので打ち切り
            break
    return addr, args


def ntp_to_epoch(timetag: int):
    """OSC timetag(64bit NTP) → unix epoch 秒。1 = immediately → None。"""
    if timetag == 1 or timetag == 0:
        return None
    sec = timetag >> 32
    frac = timetag & 0xFFFFFFFF
    return sec - _NTP_EPOCH_OFFSET + frac / 2 ** 32


def parse_osc_packet(data: bytes, _at=None):
    """メッセージまたはバンドル(入れ子可) → [(address, args, at_or_None), ...]。
    バンドルの timetag が未来なら at として各メッセージに付く(過去/immediate は None)。"""
    if data[:8] == b"#bundle\0":
        tt = struct.unpack_from(">Q", data, 8)[0]
        at = ntp_to_epoch(tt)
        if at is not None and at <= time.time():
            at = None                    # 過去のtimetag = 即時実行(OSC 1.0の規定)
        out = []
        i = 16
        while i + 4 <= len(data):
            n = struct.unpack_from(">i", data, i)[0]; i += 4
            out.extend(parse_osc_packet(data[i:i + n], at if at is not None else _at))
            i += n
        return out
    addr, args = parse_osc_message(data)
    return [(addr, args, _at)]


# 送信側実装用(テスト・ツール): メッセージ/バンドルの組み立て
def build_osc_message(addr: str, *args) -> bytes:
    def pad(b):
        return b + b"\0" * (4 - len(b) % 4)
    tags = ","
    body = b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"; body += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; body += struct.pack(">f", a)
        elif isinstance(a, bytes):
            tags += "b"; body += struct.pack(">i", len(a)) + pad(a) if len(a) % 4 else struct.pack(">i", len(a)) + a
        else:
            tags += "s"; body += pad(str(a).encode())
    return pad(addr.encode()) + pad(tags.encode()) + body


def build_osc_bundle(at_epoch, *messages: bytes) -> bytes:
    if at_epoch is None:
        tt = 1
    else:
        sec = int(at_epoch) + _NTP_EPOCH_OFFSET
        frac = int((at_epoch - int(at_epoch)) * 2 ** 32) & 0xFFFFFFFF
        tt = (sec << 32) | frac
    out = b"#bundle\0" + struct.pack(">Q", tt)
    for m in messages:
        out += struct.pack(">i", len(m)) + m
    return out


OSC_ADDRESSES = {
    "/soluna/cue":        "<url:s> [lead:f] [gain:f]      → POST /api/cue",
    "/soluna/preload":    "<url:s>                        → 事前配布(再生しない)",
    "/soluna/stop":       "                               → cue stop",
    "/soluna/go":         "                               → show NEXT",
    "/soluna/show/goto":  "<i:i>                          → 次のGOで i 番を発火",
    "/soluna/light":      "<pattern:s> [c1:s] [c2:s] [bpm:f] → POST /api/light",
    "/soluna/light/stop": "                               → light stop",
    "/soluna/align":      "<ms:f>                         → 全ゾーン一括トリム",
    "/soluna/zone":       "<name:s> <delay_ms:f>          → 1ゾーンの遅延を更新",
    "/soluna/tc":         "<HH:MM:SS:FF:s> [fps:f]        → 「今この瞬間の番組TC」",
}


class OscServer(asyncio.DatagramProtocol):
    """UDP受け口。dispatch(address, args, at, ch) を awaitable で呼ぶ。
    末尾の文字列引数 'ch=<name>' があればチャンネル指定として剥がす。"""

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            items = parse_osc_packet(data)
        except Exception as e:                        # 壊れたパケットで落ちない
            print(f"[osc] bad packet from {addr}: {e}")
            return
        for address, args, at in items:
            ch = "festival"
            if args and isinstance(args[-1], str) and args[-1].startswith("ch="):
                ch = args[-1][3:] or "festival"
                args = args[:-1]
            asyncio.ensure_future(self._run(address, args, at, ch, addr))

    async def _run(self, address, args, at, ch, addr):
        try:
            r = await self.dispatch(address, args, at, ch)
            print(f"[osc] {addr[0]} {address} {args} → {r}")
        except Exception as e:
            print(f"[osc] {address} {args} failed: {e}")

    @classmethod
    async def start(cls, dispatch, port: int, host="0.0.0.0"):
        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            lambda: cls(dispatch), local_addr=(host, port), reuse_port=False)
        return transport, proto


# ---------------------------------------------------------------------------
# Timecode (SMPTE 12M) — 24 / 25 / 30 / 29.97 drop-frame
# ---------------------------------------------------------------------------
def tc_rate(fps: float, drop: bool):
    """公称fps → 実フレームレート。29.97/59.94 は 30000/1001 系、23.976 は 24000/1001。"""
    f = float(fps)
    if abs(f - 29.97) < 0.02:
        return 30000.0 / 1001.0, 30, True if drop is None else drop
    if abs(f - 59.94) < 0.02:
        return 60000.0 / 1001.0, 60, True if drop is None else drop
    if abs(f - 23.976) < 0.02 or abs(f - 23.98) < 0.02:
        return 24000.0 / 1001.0, 24, False
    n = int(round(f))
    return float(n), n, bool(drop)


def parse_tc(tc: str):
    """'01:02:03:04' / '01:02:03;04'(;=ドロップフレーム) → (h,m,s,f, drop_hint)。"""
    s = str(tc).strip()
    drop = ";" in s or "." in s.replace(":", "", 2)
    parts = s.replace(";", ":").replace(".", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"timecode must be HH:MM:SS:FF (got {tc!r})")
    h, m, sec, f = (int(p) for p in parts)
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60 and 0 <= f < 60):
        raise ValueError(f"timecode out of range: {tc!r}")
    return h, m, sec, f, drop


def tc_to_frames(tc: str, fps: float, drop=None) -> int:
    """タイムコード → 先頭(00:00:00:00)からのフレーム番号。
    ドロップフレーム(29.97DF): 毎分頭の 2 フレーム(;00 ;01)を飛ばす、ただし10分毎は飛ばさない。"""
    h, m, s, f, drop_hint = parse_tc(tc)
    rate, nominal, is_drop = tc_rate(fps, drop if drop is not None else (True if drop_hint else None))
    total_min = h * 60 + m
    frames = ((h * 3600 + m * 60 + s) * nominal) + f
    if is_drop:
        per_min = 2 if nominal == 30 else 4
        frames -= per_min * (total_min - total_min // 10)
    return frames


def frames_to_seconds(frames: int, fps: float, drop=None) -> float:
    rate, _n, _d = tc_rate(fps, drop)
    return frames / rate


def frames_to_tc(frames: int, fps: float, drop=None) -> str:
    """フレーム番号 → 'HH:MM:SS:FF'(DFは ';FF')。"""
    rate, nominal, is_drop = tc_rate(fps, drop)
    frames = int(frames)
    if is_drop:
        per_min = 2 if nominal == 30 else 4
        fpm = nominal * 60 - per_min               # 1分(通常)のフレーム数
        fp10 = fpm * 10 + per_min                  # 10分ブロックのフレーム数
        d = frames // fp10
        r = frames % fp10
        if r < nominal * 60:                       # 10分ブロック先頭の分(ドロップなし)
            extra_min = 0
        else:
            extra_min = (r - nominal * 60) // fpm + 1
        frames += per_min * (d * 9 + extra_min)
    ff = frames % nominal
    total_s = frames // nominal
    s = total_s % 60
    m = (total_s // 60) % 60
    h = (total_s // 3600) % 24
    sep = ";" if is_drop else ":"
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ff:02d}"


def tc_anchor(tc: str, fps: float, epoch: float, drop=None) -> dict:
    """'今(epoch)この瞬間の番組TCは tc' → 保存形。"""
    rate, nominal, is_drop = tc_rate(fps, drop)
    return {"epoch": float(epoch), "frames": tc_to_frames(tc, fps, is_drop),
            "fps": float(fps), "drop": bool(is_drop), "tc": tc}


def tc_epoch(anchor: dict, tc: str) -> float:
    """保存済みアンカー + 目標TC → サーバepoch秒。"""
    fps = anchor["fps"]
    drop = anchor.get("drop")
    frames = tc_to_frames(tc, fps, drop)
    return anchor["epoch"] + frames_to_seconds(frames - anchor["frames"], fps, drop)


# ---------------------------------------------------------------------------
# DMX out — Art-Net (ArtDMX) / sACN (E1.31)
# ---------------------------------------------------------------------------
ARTNET_PORT = 6454
SACN_PORT = 5568


def _hex2rgb(h):
    try:
        v = int(str(h).replace("#", ""), 16)
        return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
    except ValueError:
        return [255, 255, 255]


def _mix(a, b, t):
    return [a[i] + (b[i] - a[i]) * t for i in range(3)]


def _scale(c, k):
    return [int(round(max(0.0, min(255.0, v * k)))) for v in c]


def light_rgb(light: dict, t: float, x: float, jitter: float = 0.0):
    """client.html の lightColor() と同じ式。t = 同期時刻 - light.at [s], x = 会場横断位置 0..1。
    pattern=audio は端末内の音源解析が要るので DMX 側は pulse にフォールバック。"""
    cols = [_hex2rgb(c) for c in (light.get("colors") or ["#d4af37", "#7fc9a2"])]
    c0 = cols[0]
    c1 = cols[1] if len(cols) > 1 else c0
    beat = float(light.get("bpm", 120)) / 60.0
    B = float(light.get("brightness", 1.0))
    speed = float(light.get("speed", 1.0))
    pat = light.get("pattern", "pulse")
    if pat == "audio":
        pat = "pulse"
    if pat == "solid":
        return _scale(c0, B)
    if pat == "pulse":
        ph = ((t * beat) % 1 + 1) % 1
        p = math.exp(-5 * ph)
        return _scale(_mix(c0, c1, p), B * (0.06 + 0.94 * p))
    if pat == "beat":
        n = math.floor(t * beat)
        return _scale(c1 if n % 2 else c0, B)
    if pat == "wave":
        p = (math.sin(2 * math.pi * (t * 0.5 * speed - x * 1.5)) + 1) / 2
        s = p ** 3 / (p ** 3 + (1 - p) ** 3) if (p ** 3 + (1 - p) ** 3) else 0.5
        return _scale(_mix(c0, c1, s), B * (0.35 + 0.65 * abs(s - 0.5) * 2))
    if pat == "plasma":
        v = (math.sin(6.283 * (x * 1.7 + t * 0.23 * speed))
             + math.sin(6.283 * (jitter * 2.1 - t * 0.17 * speed))
             + math.sin(6.283 * (x + jitter + t * 0.11 * speed))) / 3
        p = (v + 1) / 2
        den = p ** 3 + (1 - p) ** 3
        s = p ** 3 / den if den else 0.5
        return _scale(_mix(c0, c1, s), B)
    if pat == "strobe":
        hz = min(3.0, beat)                       # 光過敏対策 3Hz 上限(端末と同じ)
        on = (t * hz) % 1 < 0.5
        return _scale(c0, B) if on else [0, 0, 0]
    return [0, 0, 0]


def dmx_frame(light, t, fixtures: int, start_ch: int = 1):
    """N台のRGB灯体を DMX 512 スロットへ(start_ch から 3ch ずつ)。"""
    data = bytearray(512)
    for i in range(fixtures):
        x = i / (fixtures - 1) if fixtures > 1 else 0.0
        jitter = (i * 0.6180339887) % 1.0         # 灯体ごとの決定論的な揺らぎ(plasma用)
        r, g, b = light_rgb(light, t, x, jitter)
        base = start_ch - 1 + i * 3
        if base + 2 >= 512:
            break
        data[base], data[base + 1], data[base + 2] = r, g, b
    return bytes(data)


def artdmx_packet(universe: int, data: bytes, seq: int = 0) -> bytes:
    """Art-Net 4 ArtDmx (OpCode 0x5000, ProtVer 14)。universe = Net(7bit)<<8 | SubUni(8bit)。"""
    length = len(data)
    if length % 2:
        data += b"\0"; length += 1
    return (b"Art-Net\0" + struct.pack("<H", 0x5000) + struct.pack(">H", 14)
            + bytes([seq & 0xFF, 0, universe & 0xFF, (universe >> 8) & 0x7F])
            + struct.pack(">H", length) + data)


def sacn_packet(universe: int, data: bytes, cid: bytes, seq: int = 0,
                source_name: str = "SOLUNA Sound", priority: int = 100) -> bytes:
    """E1.31 (sACN) data packet: root / framing / DMP の3層。start code 0 + 512 slots。"""
    slots = data[:512].ljust(512, b"\0")
    prop_count = 1 + len(slots)
    dmp = (struct.pack(">H", 0x7000 | (10 + 1 + len(slots)))       # DMP length = 11 + slots
           + bytes([0x02, 0xA1]) + struct.pack(">HHH", 0x0000, 0x0001, prop_count)
           + b"\0" + slots)
    framing_len = 77 + len(dmp)
    framing = (struct.pack(">H", 0x7000 | framing_len) + struct.pack(">I", 0x00000002)
               + source_name.encode()[:63].ljust(64, b"\0")
               + bytes([priority & 0xFF]) + struct.pack(">H", 0) + bytes([seq & 0xFF, 0])
               + struct.pack(">H", universe & 0xFFFF) + dmp)
    root_len = 22 + len(framing)
    root = (struct.pack(">HH", 0x0010, 0x0000) + b"ASC-E1.17\0\0\0"
            + struct.pack(">H", 0x7000 | root_len) + struct.pack(">I", 0x00000004)
            + cid[:16].ljust(16, b"\0") + framing)
    return root


def _parse_target(spec: str, default_port: int):
    """'192.168.1.255' | '192.168.1.50:3' → (ip, universe)。ポートは環境変数で別指定。"""
    ip, _, uni = spec.partition(":")
    return ip.strip(), int(uni) if uni.strip() else 0


class DmxOut:
    """進行中ライトを 40Hz で DMX 送出。get_light() が None を返したら黒を1回送って待機。"""

    def __init__(self, get_light, artnet: str = "", sacn: str = "", fixtures: int = 8,
                 artnet_port: int = ARTNET_PORT, sacn_port: int = SACN_PORT,
                 now=time.time, fps: float = 40.0, start_ch: int = 1):
        self.get_light = get_light
        self.fixtures = max(1, int(fixtures))
        self.start_ch = max(1, int(start_ch))
        self.now = now
        self.period = 1.0 / fps
        self.targets = []                  # (kind, ip, universe, port)
        if artnet:
            ip, uni = _parse_target(artnet, artnet_port)
            self.targets.append(("artnet", ip, uni, artnet_port))
        if sacn:
            ip, uni = _parse_target(sacn, sacn_port)
            self.targets.append(("sacn", ip, uni, sacn_port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setblocking(False)
        import hashlib
        self.cid = hashlib.md5(("soluna-" + socket.gethostname()).encode()).digest()
        self.seq = 0
        self.sent = 0
        self._dark = True

    def describe(self):
        return ", ".join(f"{k} {ip} u{u}:{p}" for k, ip, u, p in self.targets)

    def send(self, data: bytes):
        self.seq = (self.seq + 1) & 0xFF
        for kind, ip, uni, port in self.targets:
            pkt = (artdmx_packet(uni, data, self.seq) if kind == "artnet"
                   else sacn_packet(uni, data, self.cid, self.seq))
            try:
                self.sock.sendto(pkt, (ip, port))
                self.sent += 1
            except OSError as e:
                print(f"[dmx] send {kind} {ip}:{port} failed: {e}")

    def tick(self):
        light = self.get_light()
        if not light:
            if not self._dark:
                self.send(bytes(512))          # 消灯を1回送って以後は沈黙
                self._dark = True
            return False
        t = self.now() - float(light.get("at") or self.now())
        self.send(dmx_frame(light, t, self.fixtures, self.start_ch))
        self._dark = False
        return True

    async def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[dmx] tick failed: {e}")
            await asyncio.sleep(self.period if not self._dark else 0.1)
