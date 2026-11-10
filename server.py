#!/usr/bin/env python3
"""
SOLUNA Surround server (v3) — clock-synced, position-routed, festival-scale.

v2 まで: 1ソース→N端末のライブPCM同期 (SL2 バイナリ, playAt=サーバ時刻)。
v3 追加 (5000人フェス対応):

  * CUE MODE — 音源ファイルを事前配布し、サーバは「いつ・何を・どの音量で
    鳴らすか」だけを JSON で一斉ブロードキャストする。帯域はほぼゼロなので
    リスナー数に上限がない (5000台のスマホ = 5000個のスピーカー)。
    途中参加した端末にも接続時に進行中キューを渡す → 曲の途中から位相同期で合流。
  * ZONES — ステージからの距離ごとのゾーン表 (delay_ms) をサーバが持ち、
    接続時 config として配る。各端末は自ゾーンの遅延をかけて再生する
    (遅延 = 距離/343s + ハース系オフセット) → どこで聴いても音は
    「ステージから」聞こえたまま、目の前のスピーカーの音量で済む。
  * ADMIN API — x-soluna-admin ヘッダ (SOLUNA_ADMIN env) でキュー発火/停止・
    ゾーン更新。/admin に操作ページ。/assets/ 配下で音源を静的配信。

Wire format (binary, little-endian), header = 22 bytes:  ※v2から不変
    magic   3s   b"SL2"
    version B    = 2
    nchan   B    channels in THIS frame (1 after server extraction)
    pad     B    0
    seq     I    frame sequence (from source)
    nsamp   I    samples per channel in this frame
    playAt  d    server-epoch seconds for sample 0 (server-filled; 0 from source)
  payload: int16 interleaved PCM, nchan * nsamp samples.

HTTP:
    GET  /                 -> player (client.html), ?zone=A..F|?d=<meters>
    GET  /admin            -> cue console (admin.html)
    GET  /status           -> JSON channel/listener state
    GET  /assets/<file>    -> 事前配布する音源 (mp3/aac/opus)
    POST /api/cue?ch=X     -> {url, at?, gain?, loop?} or {stop:true}   [admin]
    POST /api/zones?ch=X   -> {zones:{A:0, B:44, ...}}  (delay_ms)      [admin]
    WS   /audio?role=push&ch=<name>     (source; first sends JSON hello {map,sr})
    WS   /audio?role=listen&ch=<name>&pos=L|&zone=B|&d=35
"""
import asyncio
import json
import math
import random
import shutil
import socket
import struct
import subprocess
import time
import os
from urllib.parse import quote
from aiohttp import web, WSMsgType
import showctl          # OSC in / timecode / Art-Net・sACN out(依存ゼロ)

SR = 48000
# live mode: schedule this far ahead of "now"。既存ハウスPAとの融合時は
# 「パイプライン遅延 ≦ グリッド最前ゾーンの物理遅延(d/343)」が条件なので
# 有線LANなら SOLUNA_LEAD=0.08 程度まで詰める。push hello {"lead":..} でも上書き可。
LEAD = float(os.environ.get("SOLUNA_LEAD", "0.6"))
CUE_LEAD = 3.0                  # cue mode: default lead so every device can arm
HEADER = struct.Struct("<3sBBBIId")
HERE = os.path.dirname(os.path.abspath(__file__))
# SOLUNA_DATA_DIR: 音源とstate.jsonを置く永続ディレクトリ(fly volume=/data 等)。
# 未設定ならリポジトリ直下(自宅/デモ)。再デプロイで曲が消える構造をここで潰す。
DATA_DIR = os.environ.get("SOLUNA_DATA_DIR") or HERE
ASSETS = os.path.join(DATA_DIR, "assets")
STATE_FILE = os.path.join(DATA_DIR, "state.json")   # クラッシュセーフ: ショー状態の正本
# 音源のCDN/R2前置き: 設定すると端末は /assets/x を <ASSET_BASE>/x から取る
# (5000台のPRELOADバーストを単一VMに当てない)。サーバ自身の /assets/ も残る。
ASSET_BASE = (os.environ.get("SOLUNA_ASSET_BASE") or "").rstrip("/")
# 公開URLの正本: QRや案内ページに載せるドメイン。未設定なら request.host(fly.dev等)。
# 会場で配るQRが別ドメイン(fly.dev)を指す事故を防ぐため、本番は sound.solun.art を固定する。
PUBLIC_BASE = (os.environ.get("SOLUNA_PUBLIC_BASE") or "").rstrip("/")
STARTED_AT = time.time()
VERSION = "v7"

# ---- クロック源: wall clockではなく monotonic ----------------------------------
# time.time() はホストのNTPステップで飛ぶ(=全端末が一斉に再同期して音がジャンプ)。
# 起動時に一度だけ epoch を取り、以後は monotonic で進める。ドリフトは数ppm(/hで数十ms)
# で、5000台の相対同期には影響しない(全員が同じ時計を見るため)。
_EPOCH0 = time.time() - time.monotonic()


def now() -> float:
    return _EPOCH0 + time.monotonic()

# 既定ゾーン: ステージからの距離[m] → delay_ms = d/343*1000 + 15ms (Haas)。
# 実会場では /api/zones で実測距離に合わせて上書きする。
def default_zones():
    return {z: round(d / 343.0 * 1000.0 + 15.0, 1)
            for z, d in {"A": 0, "B": 15, "C": 30, "D": 45, "E": 60, "F": 80}.items()}

# channel_name -> state
channels: dict = {}


def _chan(name):
    return channels.setdefault(name, {
        "source": None,           # push ws
        "map": ["L", "R", "C"],   # position -> index order (live mode)
        "sr": SR,
        "epoch": None,            # server-epoch for sample 0 of the stream
        "played": 0,              # cumulative samples emitted
        "seq": 0,
        "listeners": {},          # ws -> {"pos":..., "zone":..., "d":...}
        "zones": default_zones(),
        "zone_gain_db": {},       # ゾーン別音量補正[dB](FOHが遠いゾーンを持ち上げる等)
        "base_ms": 0.0,           # 全ゾーン一括トリム(既存ハウスPAとの位相合わせ)
        "lead": LEAD,
        "cue": None,              # active cue dict (途中参加者へ再送する)
        "light": None,            # active light dict (SOLUNAモード: 色の同期)
        "light_presets": {},      # 名前付きライトプリセット {name -> look}(/admin・再起動で復元)
        "light_off_at": None,     # 自動消灯の予約時刻(server epoch秒)。None=予約なし
        "geo": None,              # {"lat","lng"} ステージ位置(GPSゾーン自動選択用)
        "preload": None,          # 事前配布済み音源URL(FIRE前のDLバースト回避)
        "show": None,             # セットリスト [{label,url,video,light},...]
        "show_i": -1,             # 進行位置(次のNEXTで show_i+1 を発火)
        "show_auto": False,       # 自動進行(単体フェス: 誰もNEXTを押さなくても一晩回る)
        "show_gap": 1.0,          # 曲間の空白[s](自動進行時)
        "show_next_at": None,     # 自動進行で次を発火する予定時刻(server epoch秒)。None=予定なし
        "net": None,              # 回線案内 {"ssid","wifi_zones":[..]} 前方=会場Wi-Fi/後方=LTE
        "node_cfg": {},           # Piノードの割当 host -> {"zone","pos","gain_db"}(/adminから・接続時に再送)
        "tc": None,               # タイムコード基準 {"epoch","frames","fps","drop","tc"}(cue を tc で指定)
        "mute": False,            # FOHキルスイッチ(全端末・全ノード・CUE/LIVE両方)
        "level": None,            # LIVE入力メータ {"peak_dbfs","rms_dbfs","clip","t"}(FOHが「来てる」を目で見る)
        "stats": {"peak_listeners": 0, "peak_playing": 0, "peak_playing_zone": {}, "cues": 0,
                  "lights": 0, "peak_lit": 0, "first_cue": None, "last_cue": None, "started": None},   # 事後レポート用
    })


# ---- Pi ボックス台帳(agent.py が5秒ごとに POST /api/nodes/report) ----------------
# チャンネル非依存(箱は1台)。stale=最後の報告から SOLUNA_NODE_STALE_S 秒超。
nodes: dict = {}
_node_rate: dict = {}
NODE_STALE_S = float(os.environ.get("SOLUNA_NODE_STALE_S", "15"))


# ---- クラッシュセーフ: 電源断でもサウンドチェックとショー進行を失わない ----------
_PERSIST = ("map", "sr", "zones", "zone_gain_db", "base_ms", "lead", "cue", "light",
            "geo", "preload", "show", "show_i", "net", "node_cfg", "tc", "mute", "stats",
            "show_auto", "show_gap", "show_next_at", "light_presets", "light_off_at")


def _save_state():
    try:
        out = {name: {k: st.get(k) for k in _PERSIST} for name, st in channels.items()}
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(out, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        print(f"[state] save failed: {e}")


def _load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for name, vals in data.items():
            st = _chan(name)
            for k in _PERSIST:
                if k in vals:
                    st[k] = vals[k]
        print(f"[state] restored {len(data)} channel(s) from {STATE_FILE}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[state] load failed: {e}")


def _pos_index(state, pos):
    m = state["map"]
    pos = (pos or "").upper()
    if pos in m:
        return m.index(pos)
    try:
        i = int(pos)
        if 0 <= i < len(m):
            return i
    except (ValueError, TypeError):
        pass
    return 0


def _public_base(request):
    """QR・案内ページに載せる公開URL。SOLUNA_PUBLIC_BASE があればそれを正とする。"""
    return PUBLIC_BASE or f"https://{request.host}"


def _config_msg(state):
    return json.dumps({"t": "config", "zones": state["zones"], "sr": state["sr"],
                       "base_ms": state["base_ms"], "geo": state["geo"],
                       "zone_gain_db": state.get("zone_gain_db") or {},
                       "asset_base": ASSET_BASE or None, "public_base": PUBLIC_BASE or None,
                       "ver": VERSION,
                       "net": state.get("net"), "preload": state.get("preload"),
                       "server_ms": now() * 1000.0})


async def _broadcast_text(state, text):
    # 並列送信+タイムアウト: 死にかけの1端末が全体の配信を詰まらせないように
    async def _send(ws):
        try:
            await asyncio.wait_for(ws.send_str(text), timeout=2.0)
        except Exception:
            state["listeners"].pop(ws, None)
    await asyncio.gather(*(_send(ws) for ws in list(state["listeners"].keys())))


def _admin_ok(request):
    tok = os.environ.get("SOLUNA_ADMIN")
    return bool(tok) and request.headers.get("x-soluna-admin") == tok


async def audio_ws(request):
    role = request.query.get("role", "listen")
    name = request.query.get("ch", "default")
    # koe* チャンネルは声コマンドに直結する(ブリッジがClaude Codeへタイプ)ため、
    # SOLUNA_TOKEN が設定されていればトークン必須。それ以外のchは従来どおり素通し。
    tok = os.environ.get("SOLUNA_TOKEN")
    if tok and name.startswith("koe") and request.query.get("token") != tok:
        raise web.HTTPForbidden(text="token required for koe* channels")
    # 本番の音の乗っ取り防止: SOLUNA_DJ_TOKEN が設定されていれば配信側は要トークン
    # (リスナーは常にオープン)。未設定なら従来どおり=自宅/デモの手軽さ優先。
    dj_tok = os.environ.get("SOLUNA_DJ_TOKEN")
    if (dj_tok and role == "push" and not name.startswith("koe")
            and request.query.get("token") != dj_tok):
        raise web.HTTPForbidden(text="broadcast token required (SOLUNA_DJ_TOKEN)")
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=30)
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
                            if hello.get("lead") is not None:
                                state["lead"] = max(0.02, float(hello["lead"]))
                            print(f"[push] ch={name} map={state['map']} "
                                  f"sr={state['sr']} lead={state['lead']}")
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
    meta = {"pos": request.query.get("pos", "L"),
            "zone": (request.query.get("zone") or "").upper() or None,
            "d": request.query.get("d"),
            "host": (request.query.get("host") or "")[:64] or None}   # Piノードは自分のhostnameを名乗る
    state["listeners"][ws] = meta
    print(f"[listen] +{meta} ch={name} (n={len(state['listeners'])})")
    try:
        await ws.send_str(_config_msg(state))
        if state.get("preload") and not state["cue"]:   # 開演前に入場 → 先にDLさせる
            await ws.send_str(json.dumps({"t": "preload", "url": state["preload"]}))
        if state["cue"]:                      # 途中参加 → 進行中キューを渡す
            await ws.send_str(json.dumps({"t": "cue", **state["cue"]}))
        if state["light"]:                    # 途中参加 → 進行中ライトも渡す
            await ws.send_str(json.dumps({"t": "light", **state["light"]}))
        if state.get("mute"):                 # ミュート中に入場 → 黙って入る
            await ws.send_str(json.dumps({"t": "mute", "on": True}))
        _track_peaks(state)
        if meta["host"] and (state.get("node_cfg") or {}).get(meta["host"]):
            cfg = state["node_cfg"][meta["host"]]   # /admin で割り当てたゾーンを再送(node.json消失でも復元)
            await ws.send_str(json.dumps({"t": "assign", **cfg}))
            if cfg.get("zone"):
                meta["zone"] = str(cfg["zone"]).upper()
            if cfg.get("pos"):
                meta["pos"] = str(cfg["pos"]).upper()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get("t") == "ping":
                    await ws.send_str(json.dumps({
                        "t": "pong", "c": m.get("c"), "s": now() * 1000.0
                    }))
                elif m.get("t") == "pos":
                    meta["pos"] = m.get("pos", meta["pos"])
                elif m.get("t") == "zone":
                    meta["zone"] = (m.get("zone") or "").upper() or None
                elif m.get("t") == "report":
                    # 端末の実状態(FOHの観測性): preloaded/playing/idle/failed、
                    # AudioContext状態、電池、同期精度。位置情報は受け取らない。
                    if now() - state.get("_peak_t", 0) > 5.0:   # 事後レポート用ピーク(5秒に1回・安価)
                        state["_peak_t"] = now()
                        _track_peaks(state)
                    meta["rep"] = {
                        "st": str(m.get("st") or "idle")[:12],
                        "ctx": str(m.get("ctx") or "")[:12],
                        "bat": (None if m.get("bat") is None
                                else max(0.0, min(1.0, float(m["bat"])))),
                        "acc": (None if m.get("acc") is None else float(m["acc"])),
                        "cue": str(m.get("cue") or "")[:40],
                        "kind": str(m.get("kind") or "phone")[:8],
                        "lit": bool(m.get("lit")),   # LIGHT v2: 実際に光っている端末
                        "t": now(),
                    }
                    if m.get("host"):
                        meta["host"] = str(m["host"])[:64]
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        state["listeners"].pop(ws, None)
        print(f"[listen] -{meta.get('zone') or meta.get('pos')} ch={name} "
              f"(n={len(state['listeners'])})")
    return ws


async def _fanout(name, state, data: bytes):
    if len(data) < HEADER.size:
        return
    magic, ver, nchan, _pad, seq, nsamp, _playat = HEADER.unpack_from(data, 0)
    if magic != b"SL2" or nchan < 1 or nsamp < 1:
        return
    body = memoryview(data)[HEADER.size:]
    total = nchan * nsamp
    import array
    pcm = array.array("h")
    pcm.frombytes(body[: total * 2].tobytes())
    if len(pcm) < total:
        return

    t_now = now()
    # LIVE入力メータ(FOH向け): 8サンプルおきの間引きで安価に peak/RMS。clip=フルスケール到達
    if seq % 5 == 0:                          # 100ms に1回
        import math
        sub = pcm[::8]
        peak = max(abs(v) for v in sub) if len(sub) else 0
        rms = math.sqrt(sum(v * v for v in sub) / len(sub)) if len(sub) else 0.0
        state["level"] = {"peak_dbfs": round(20 * math.log10(max(peak, 1) / 32768.0), 1),
                          "rms_dbfs": round(20 * math.log10(max(rms, 1.0) / 32768.0), 1),
                          "clip": peak >= 32767, "t": t_now}
    if state["epoch"] is None:
        state["epoch"] = t_now + state["lead"]
        state["played"] = 0
    play_at = state["epoch"] + state["played"] / float(state["sr"])
    state["played"] += nsamp
    state["seq"] = seq

    # Build one mono frame per distinct position in use, then send in parallel
    # (直列だと遅い1端末が全ノードの位相を巻き込む)。
    cache = {}

    async def _send(ws, meta):
        idx = _pos_index(state, meta.get("pos"))
        if idx >= nchan:
            idx = 0
        frame = cache.get(idx)
        if frame is None:
            mono = pcm[idx::nchan]           # de-interleave this channel
            hdr = HEADER.pack(b"SL2", 2, 1, 0, seq, nsamp, play_at)
            frame = hdr + mono.tobytes()
            cache[idx] = frame
        try:
            await asyncio.wait_for(ws.send_bytes(frame), timeout=1.0)
        except Exception:
            state["listeners"].pop(ws, None)

    await asyncio.gather(*(_send(ws, meta)
                           for ws, meta in list(state["listeners"].items())))


# ---- admin API -------------------------------------------------------------

class BadRequest(ValueError):
    """HTTP 400 / OSC ログ行 に共通で使う入力エラー。"""


def _cue_at(state, body):
    """cue の発火時刻: at(epoch秒) > tc(番組タイムコード) > lead(秒後)。"""
    if body.get("at"):                       # サーバepoch秒を直接指定
        return float(body["at"])
    if body.get("tc"):                       # 番組タイムコードで指定(/api/timecode で基準を教えてあること)
        anchor = state.get("tc")
        if not anchor:
            raise BadRequest("tc given but no timecode anchor: POST /api/timecode first")
        try:
            return showctl.tc_epoch(anchor, str(body["tc"]))
        except ValueError as e:
            raise BadRequest(str(e))
    return now() + float(body.get("lead") or CUE_LEAD)


async def do_cue(state, body: dict) -> dict:
    """POST /api/cue と OSC /soluna/cue が共有する本体。"""
    if body.get("stop"):
        state["cue"] = None
        _auto_cancel(state)                      # STOP = ショーの自動進行も止める
        _save_state()
        await _broadcast_text(state, json.dumps({"t": "cue_stop"}))
        return {"ok": True, "stopped": True, "listeners": len(state["listeners"])}

    url = body.get("url")            # 音声(WebAudio=サンプル精度)
    video = body.get("video")        # 映像(video要素=クロックにドリフト補正で追従)
    if not url and not video:
        raise BadRequest("url (audio) and/or video required")

    if body.get("preload"):
        url = url or video
        # 事前配布: 全端末がDL/デコードだけ済ませる(再生しない)。本番のFIREは
        # 一斉DLバーストなしで頭から揃う。開演30分前に打っておくのが正。
        state["preload"] = url
        _save_state()
        await _broadcast_text(state, json.dumps({"t": "preload", "url": url}))
        return {"ok": True, "preloaded": url, "listeners": len(state["listeners"])}
    at = _cue_at(state, body)
    cue = {
        "id": body.get("id") or f"cue-{int(at * 1000)}",
        "at": at,                                # server-epoch seconds (sample 0)
        "gain": float(body.get("gain", 1.0)),
        "loop": bool(body.get("loop", False)),
    }
    if url:
        cue["url"] = url
    if video:
        cue["video"] = video
    if body.get("zones"):                    # ウォークテスト: 指定ゾーンのみ再生
        cue["zones"] = [str(z).upper() for z in body["zones"]]
    for k in ("title", "artist", "image"):   # NOW PLAYING 表示 / スポンサー静止画(端末+/screen)
        if body.get(k):
            cue[k] = str(body[k])[:200]
    state["cue"] = cue
    st = state["stats"]
    st["cues"] += 1
    st["first_cue"] = st["first_cue"] or at
    st["last_cue"] = at
    _save_state()
    await _broadcast_text(state, json.dumps({"t": "cue", **cue}))
    return {"ok": True, "cue": cue, "listeners": len(state["listeners"])}


def _admin_state(request):
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    return _chan(request.query.get("ch", "festival"))


async def api_cue(request):
    state = _admin_state(request)
    try:
        return web.json_response(await do_cue(state, await request.json()))
    except BadRequest as e:
        raise web.HTTPBadRequest(text=str(e))


LIGHT_PATTERNS = ("solid", "pulse", "beat", "wave", "plasma", "strobe", "audio")


def _light_from(body: dict, id_prefix="light") -> dict:
    pattern = body.get("pattern", "pulse")
    if pattern not in LIGHT_PATTERNS:
        raise BadRequest("pattern must be one of " + "|".join(LIGHT_PATTERNS))
    light = {
        "id": body.get("id") or f"{id_prefix}-{int(now() * 1000)}",
        "pattern": pattern,
        "colors": body.get("colors") or ["#d4af37", "#7fc9a2"],
        "bpm": float(body.get("bpm", 120)),
        "speed": float(body.get("speed", 1.0)),
        "brightness": float(body.get("brightness", 1.0)),
        "at": float(body.get("at") or now()),   # パターン位相の基準時刻
    }
    # --- LIGHT v2: ゾーン限定・フェード・トーチ(未指定なら載せない=帯域節約) ---
    zones = body.get("zones")
    if zones:
        zs = [str(z).upper()[:8] for z in zones if str(z).strip()]
        if zs:
            light["zones"] = zs
    fade_ms = body.get("fade_ms")
    if fade_ms:
        light["fade_ms"] = int(max(0, min(10000, float(fade_ms))))
    if body.get("torch"):
        light["torch"] = True
    return light


def _clamp_look(body: dict) -> dict:
    """プリセット保存用に見た目フィールドだけを検証・正規化する。"""
    pattern = body.get("pattern", "pulse")
    if pattern not in LIGHT_PATTERNS:
        raise BadRequest("pattern must be one of " + "|".join(LIGHT_PATTERNS))
    return {
        "pattern": pattern,
        "colors": body.get("colors") or ["#d4af37", "#7fc9a2"],
        "bpm": float(body.get("bpm", 120)),
        "speed": float(body.get("speed", 1.0)),
        "brightness": float(body.get("brightness", 1.0)),
    }


async def do_light(state, body: dict) -> dict:
    """POST /api/light と OSC /soluna/light が共有する本体。DMX 出力は state["light"] を
    40Hz で読む showctl.DmxOut が拾うので、ここでは状態更新+配信だけ。"""
    if body.get("stop"):
        _light_auto_cancel(state)
        _zone_auto_cancel(state)
        _timeline_cancel(state)
        if body.get("fade_ms"):
            _fade_hint = int(max(0, min(10000, float(body["fade_ms"]))))
        else:
            _fade_hint = (state["light"] or {}).get("fade_ms") or 0
        state["light"] = None
        _save_state()
        stop_msg = {"t": "light_stop"}
        if _fade_hint:
            stop_msg["fade_ms"] = _fade_hint
        await _broadcast_text(state, json.dumps(stop_msg))
        return {"ok": True, "stopped": True, "listeners": len(state["listeners"])}
    light = _light_from(body)
    state["light"] = light
    _timeline_cancel(state)                      # 手動ライトは曲中タイムラインを上書きする
    # LIGHT v2: 自動消灯(auto_off_s 秒後に停止。then='next' でショー次へ)
    auto_off = body.get("auto_off_s", body.get("auto_off"))
    if auto_off is not None:
        _light_auto_schedule(state, now() + max(0.0, float(auto_off)), body.get("then"))
    else:
        _light_auto_cancel(state)
    # LIGHT v2: ゾーン自動巡回(zones_auto) — 光が会場を勝手に動き回る
    za = body.get("zones_auto")
    if za:
        cfg = za if isinstance(za, dict) else {"mode": str(za)}
        if body.get("fade_ms") and not cfg.get("fade_ms"):
            cfg["fade_ms"] = body["fade_ms"]
        if body.get("torch"):
            cfg["torch"] = True
        _zone_auto_start(state, cfg)
    else:
        _zone_auto_cancel(state)
    state["stats"]["lights"] += 1
    _save_state()
    await _broadcast_text(state, json.dumps({"t": "light", **light}))
    return {"ok": True, "light": light, "listeners": len(state["listeners"]),
            "auto_off_at": state.get("light_off_at"), "zones_auto": state.get("zones_auto")}


async def api_light(request):
    """SOLUNAモード(色の同期)。色データは配信せず「パターン+開始時刻」だけを
    配り、各端末が (ゾーン位置, 同期時刻) から色をローカル計算する — 音のCUEと
    同じ思想で帯域ゼロ・台数無制限。
    body: {pattern: solid|pulse|wave|plasma|strobe|audio,
           colors: ["#rrggbb", ...], bpm, speed, brightness, at?,
           zones?: ["A","B"], fade_ms?: int, torch?: bool} or {stop:true, fade_ms?}
    pattern=audio は再生中キューの音源から端末側がエネルギー包絡を解析して
    明るさに変換する(追加通信なし)。"""
    state = _admin_state(request)
    try:
        return web.json_response(await do_light(state, await request.json()))
    except BadRequest as e:
        raise web.HTTPBadRequest(text=str(e))


async def api_light_presets(request):
    """名前付きライトプリセットの保存/取得/削除(チャンネル別・再起動で復元)。
    GET → {"ok":true,"presets":{name:look}}
    POST {"name":"Warm Pulse", ...look} → 保存 / {"name":"X","delete":true} → 削除。"""
    state = _admin_state(request)
    presets = state.setdefault("light_presets", {})
    if request.method == "GET":
        return web.json_response({"ok": True, "presets": presets})
    body = await request.json()
    name = str(body.get("name") or "").strip()[:40]
    if not name:
        raise web.HTTPBadRequest(text="name required")
    if body.get("delete"):
        presets.pop(name, None)
    else:
        try:
            presets[name] = _clamp_look(body)
        except BadRequest as e:
            raise web.HTTPBadRequest(text=str(e))
    _save_state()
    return web.json_response({"ok": True, "name": name, "presets": presets})


def _wav_duration(url):
    """/assets/x.wav の長さ[s]を RIFF ヘッダから(純Python・安価)。wav 以外/失敗は None。
    主経路は admin が decodeAudioData で測って step.dur に入れる方(mp3/aac はそちら)。"""
    if not url or not url.lower().endswith(".wav") or not url.startswith("/assets/"):
        return None
    path = os.path.join(ASSETS, os.path.basename(url))
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        i, byte_rate, data_len = 12, None, None
        while i + 8 <= len(head):
            cid, size = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
            if cid == b"fmt ":
                byte_rate = struct.unpack("<I", head[i + 16:i + 20])[0]
            elif cid == b"data":
                data_len = size
                break
            i += 8 + size + (size & 1)
        if byte_rate and data_len:
            return round(data_len / byte_rate, 3)
    except Exception:
        pass
    return None


def _auto_cancel(state):
    """自動進行の予約を取り消す(手動NEXT/goto/reset/stop/auto OFF/セットリスト差し替えで必ず呼ぶ)。"""
    t = state.get("_auto_task")
    if t is not None and not t.done():
        t.cancel()
    state["_auto_task"] = None
    state["show_next_at"] = None


def _light_auto_cancel(state):
    """自動消灯の予約を取り消す(新しいライト・手動stop・チャンネル削除で呼ぶ)。"""
    t = state.get("_light_task")
    if t is not None and not t.done():
        t.cancel()
    state["_light_task"] = None
    state["light_off_at"] = None


async def _light_auto_off(state, off_at):
    """off_at(server epoch秒)に達したら自動で消灯。then='next' なら続けてショーの次へ。"""
    try:
        await asyncio.sleep(max(0.0, off_at - now()))
        if state.get("light_off_at") != off_at or not state.get("light"):
            return                               # 取り消し済み/差し替え済み
        state["_light_task"] = None
        state["light_off_at"] = None
        fade = (state["light"] or {}).get("fade_ms") or 0
        state["light"] = None
        _save_state()
        msg = {"t": "light_stop"}
        if fade:
            msg["fade_ms"] = int(fade)
        await _broadcast_text(state, json.dumps(msg))
        if state.get("light_then") == "next":
            state["light_then"] = None
            await do_show(state, {"next": True, "_auto": True})
    except asyncio.CancelledError:
        pass
    except Exception as e:                       # noqa: BLE001
        print(f"[light] auto-off failed: {e}")


def _light_auto_schedule(state, off_at, then=None):
    """off_at に自動消灯を予約。0/None なら解除のみ。then='next' で消灯後にショー次へ。"""
    _light_auto_cancel(state)
    if not off_at or off_at <= now():
        return None
    state["light_off_at"] = off_at
    state["light_then"] = then if then == "next" else None
    state["_light_task"] = asyncio.get_event_loop().create_task(_light_auto_off(state, off_at))
    return off_at


def _light_resume_all():
    """再起動後: 自動消灯の予約が残っていれば復元(時刻が過ぎていれば即消灯)。on_startup から呼ぶ。"""
    for st in channels.values():
        off = st.get("light_off_at")
        if off and st.get("light"):
            st["_light_task"] = asyncio.get_event_loop().create_task(_light_auto_off(st, off))
            print(f"[light] auto-off resumed: in {max(0.0, off - now()):.1f}s")


# ---- ゾーン自動巡回 (zones_auto): 光が会場を勝手に動き回る --------------------
# サーバが一定間隔で「点灯するゾーンの組」を計算し直し、lightを再配信する。
# 端末側は zones を見て自分のゾーンだけ光る(v2)ので、追加実装は不要。
ZONE_AUTO_MODES = ("sweep", "chase", "random", "bounce")


def _zone_auto_cancel(state):
    t = state.get("_zone_auto_task")
    if t is not None and not t.done():
        t.cancel()
    state["_zone_auto_task"] = None
    state["zones_auto"] = None


def _zone_auto_frame(mode, keys, i, width):
    """i番目のフレームで点灯するゾーン集合を返す(純関数)。"""
    n = len(keys)
    if n == 0:
        return []
    width = max(1, min(n, int(width or 1)))
    if mode == "chase":
        return [keys[i % n]]
    if mode == "bounce":
        period = max(1, 2 * n - 2)
        k = i % period
        idx = k if k < n else period - k
        return [keys[idx % n]]
    if mode == "random":
        return [random.choice(keys)]
    # sweep: width 個の窓が前から後ろへ流れ、端で折り返す
    start = i % n
    return [keys[(start + w) % n] for w in range(width)]


async def _zone_auto_run(state, cfg):
    """cfg = {mode, period_s, width, fade_ms, torch}。period_s ごとにフレームを進めて再配信。
    light が消されたら止まる。"""
    keys = list((state.get("zones") or {}).keys())
    i = 0
    try:
        while state.get("zones_auto") is cfg and state.get("light"):
            base = state["light"]
            zs = _zone_auto_frame(cfg["mode"], keys, i, cfg.get("width", 1))
            light = dict(base)
            light["zones"] = zs
            light["id"] = f"{base.get('id', 'light')}-za{i}"
            light["at"] = now()
            if cfg.get("fade_ms"):
                light["fade_ms"] = int(cfg["fade_ms"])
            if cfg.get("torch"):
                light["torch"] = True
            state["light"] = light
            await _broadcast_text(state, json.dumps({"t": "light", **light}))
            i += 1
            await asyncio.sleep(max(0.05, float(cfg.get("period_s") or 0.6)))
    except asyncio.CancelledError:
        pass
    except Exception as e:                       # noqa: BLE001
        print(f"[light] zones_auto failed: {e}")


def _zone_auto_start(state, cfg):
    _zone_auto_cancel(state)
    cfg = {"mode": cfg.get("mode", "sweep"), "period_s": cfg.get("period_s", 0.6),
           "width": cfg.get("width", 1), "fade_ms": cfg.get("fade_ms") or 0,
           "torch": bool(cfg.get("torch"))}
    if cfg["mode"] not in ZONE_AUTO_MODES:
        cfg["mode"] = "sweep"
    state["zones_auto"] = cfg
    state["_zone_auto_task"] = asyncio.get_event_loop().create_task(_zone_auto_run(state, cfg))
    return cfg


# ---- ステップ内ライト・タイムライン (timeline): 1曲の中で演出が切り替わる ----
# DJ42の各曲は「イントロ→ハープ→キック→ウェーブ→…→ドロップ→アウトロ」と
# 曲中で光の表情が変わる。step に timeline=[{at, light}, ...] を持たせると、
# サーバが cue の at(サンプル0) を基準に各時刻で light を切り替えて配信する。
def _timeline_cancel(state):
    t = state.get("_timeline_task")
    if t is not None and not t.done():
        t.cancel()
    state["_timeline_task"] = None
    state["timeline_i"] = None


async def _timeline_run(state, base_at, timeline):
    """base_at(server epoch秒=音源の0秒) を基準に、各エントリの at で light を発火。"""
    try:
        for i, ent in enumerate(timeline):
            fire_at = base_at + float(ent.get("at") or 0.0)
            await asyncio.sleep(max(0.0, fire_at - now()))
            if state.get("_timeline_task") is not asyncio.current_task():
                return                            # 差し替え/停止済み
            body = dict(ent.get("light") or {})
            body["at"] = None                     # 即時配信(サーバ時刻)
            light = _light_from(body, id_prefix=f"tl{i}")
            light["id"] = f"tl{i}"
            state["light"] = light
            state["timeline_i"] = i
            # エントリ単位のゾーン自動巡回
            za = body.get("zones_auto")
            if za:
                cfg = za if isinstance(za, dict) else {"mode": str(za)}
                if body.get("fade_ms") and not cfg.get("fade_ms"):
                    cfg["fade_ms"] = body["fade_ms"]
                if body.get("torch"):
                    cfg["torch"] = True
                _zone_auto_start(state, cfg)
            else:
                _zone_auto_cancel(state)
            await _broadcast_text(state, json.dumps({"t": "light", **light}))
    except asyncio.CancelledError:
        pass
    except Exception as e:                       # noqa: BLE001
        print(f"[light] timeline failed: {e}")


def _timeline_start(state, base_at, timeline):
    _timeline_cancel(state)
    tl = [e for e in (timeline or []) if isinstance(e, dict) and e.get("light")]
    if not tl:
        return None
    state["_timeline_task"] = asyncio.get_event_loop().create_task(_timeline_run(state, base_at, tl))
    return len(tl)


async def _auto_fire(state, next_at):
    try:
        await asyncio.sleep(max(0.0, next_at - now()))
        if state.get("show_next_at") != next_at or not state.get("show_auto"):
            return                               # 取り消し済み/差し替え済み
        state["_auto_task"] = None
        await do_show(state, {"next": True, "_auto": True})
    except asyncio.CancelledError:
        pass
    except Exception as e:                       # noqa: BLE001
        print(f"[show] auto-advance failed: {e}")


def _auto_schedule(state, at, step, i):
    """発火した step の終わり(at+dur)+gap に次のステップを予約。dur が無い/loop の step は予約しない
    (終わりが定義できない=手動で NEXT)。最後の step なら予定なし。"""
    _auto_cancel(state)
    show = state.get("show") or []
    dur = step.get("dur")
    if not state.get("show_auto") or not dur or dur <= 0 or step.get("loop") or i + 1 >= len(show):
        return None
    next_at = at + float(dur) + float(state.get("show_gap") or 0.0)
    state["show_next_at"] = next_at
    state["_auto_task"] = asyncio.get_event_loop().create_task(_auto_fire(state, next_at))
    return next_at


def _auto_resume_all():
    """再起動後: 予約が残っていれば復元(時刻が過ぎていれば即発火)。on_startup から呼ぶ。"""
    for st in channels.values():
        nxt = st.get("show_next_at")
        if st.get("show_auto") and nxt:
            st["_auto_task"] = asyncio.get_event_loop().create_task(_auto_fire(st, nxt))
            print(f"[show] auto-advance resumed: next in {max(0.0, nxt - now()):.1f}s")


async def do_show(state, body: dict) -> dict:
    """POST /api/show と OSC /soluna/go, /soluna/show/goto が共有する本体。
    auto/gap: 自動進行(各 step の dur[s]+gap 後に次を発火。dur は admin がブラウザで測る or wav ヘッダ)。"""
    touched = False
    if "no_video" in body:
        state["show_no_video"] = bool(body["no_video"])
        touched = True
    if "no_audio" in body:
        state["show_no_audio"] = bool(body["no_audio"])
        touched = True
    if "no_light" in body:
        state["show_no_light"] = bool(body["no_light"])
        touched = True
    if "auto" in body:
        state["show_auto"] = bool(body["auto"])
        if not state["show_auto"]:
            _auto_cancel(state)
        touched = True
    if body.get("gap") is not None:
        state["show_gap"] = max(0.0, min(60.0, float(body["gap"])))
        touched = True
    if body.get("steps") is not None:
        steps = []
        for raw in body["steps"]:
            step: dict = {"label": str(raw.get("label") or f"step{len(steps)+1}")[:60]}
            for k in ("url", "video", "tc"):
                if raw.get(k):
                    step[k] = str(raw[k])
            for k in ("gain", "lead"):
                if raw.get(k) is not None:
                    step[k] = float(raw[k])
            try:
                d = float(raw.get("dur") or 0)
            except (TypeError, ValueError):
                d = 0.0
            if d <= 0:
                d = _wav_duration(step.get("url")) or 0.0
            if d > 0:
                step["dur"] = round(d, 3)
            if raw.get("loop"):
                step["loop"] = True
            if isinstance(raw.get("light"), dict):
                step["light"] = raw["light"]
            if isinstance(raw.get("timeline"), list):
                step["timeline"] = raw["timeline"]
            # ステム(方式C): パート別音源のURL表 {drums,bass,other,vocals}。リモコンで選んで流す。
            if isinstance(raw.get("stems"), dict):
                step["stems"] = {str(k): str(v) for k, v in raw["stems"].items() if v}
            steps.append(step)
        state["show"] = steps
        state["show_i"] = -1
        _auto_cancel(state)
        _timeline_cancel(state)
        _save_state()
        return {"ok": True, "steps": len(steps), "auto": state["show_auto"], "gap": state["show_gap"]}

    if body.get("reset"):
        state["show_i"] = -1
        _auto_cancel(state)
        _timeline_cancel(state)
        _save_state()
        return {"ok": True, "i": 0}

    if body.get("goto") is not None:
        state["show_i"] = int(body["goto"]) - 2   # 次のNEXTで goto 番を発火
        _auto_cancel(state)
        _timeline_cancel(state)
        _save_state()
        return {"ok": True, "next": int(body["goto"])}

    if body.get("next"):
        show = state.get("show") or []
        i = state.get("show_i", -1) + 1
        if i >= len(show):
            _auto_cancel(state)
            _save_state()
            return {"ok": False, "done": True, "total": len(show)}
        step = show[i]
        state["show_i"] = i
        fired = {"i": i + 1, "total": len(show), "label": step["label"]}
        send_audio = not state.get("show_no_audio")
        send_video = not state.get("show_no_video")
        send_light = not state.get("show_no_light")
        # 音・光・自動進行の共通基準時刻。音/映像を送らなくても(光だけ運用でも)
        # タイムラインと自動進行が動くよう、分岐の外で一度だけ解決する。
        at = _cue_at(state, step)                 # lead / tc(番組タイムコード) どちらでも
        # 音/映像を送らない場合は、前の曲・映像も止める(切り忘れ防止)
        if not (send_audio or send_video):
            state["cue"] = None
            await _broadcast_text(state, json.dumps({"t": "cue_stop"}))
            fired["cue_stopped"] = True
        elif step.get("url") or step.get("video"):
            cue = {"id": f"show{i+1}-{step['label']}", "at": at,
                   "gain": float(step.get("gain", 1.0)),
                   "loop": bool(step.get("loop", False))}
            if step.get("url") and send_audio:
                cue["url"] = step["url"]
            # 方式C: 複数パート同時再生(端末が一斉に鳴らす)。単一なら url のみ。
            if step.get("urls") and send_audio:
                cue["urls"] = list(step["urls"])
            if step.get("video") and send_video:
                cue["video"] = step["video"]
            state["cue"] = cue
            await _broadcast_text(state, json.dumps({"t": "cue", **cue}))
            fired["cue"] = cue["id"]
        # 曲中タイムライン: at(サンプル0) を基準に演出を切り替える(光を送るときだけ)。
        # 音・映像OFFの光だけ運用でも動くよう、cue の有無に依存させない。
        if step.get("timeline") and send_light:
            n = _timeline_start(state, at, step["timeline"])
            if n:
                fired["timeline"] = n
        else:
            _timeline_cancel(state)
        # 光を送らない場合は前の光も消す
        if not send_light:
            _timeline_cancel(state)
            _zone_auto_cancel(state)
            state["light"] = None
            await _broadcast_text(state, json.dumps({"t": "light_stop"}))
            fired["light_stopped"] = True
        elif step.get("light"):
            light = _light_from(dict(step["light"], at=None), id_prefix=f"showlight{i+1}")
            light["id"] = f"showlight{i+1}"
            state["light"] = light
            await _broadcast_text(state, json.dumps({"t": "light", **light}))
            fired["light"] = light["pattern"]
            # LIGHT v2: step に zones_auto があれば自動巡回を開始
            za = step["light"].get("zones_auto")
            if za:
                cfg = za if isinstance(za, dict) else {"mode": str(za)}
                if step["light"].get("fade_ms") and not cfg.get("fade_ms"):
                    cfg["fade_ms"] = step["light"]["fade_ms"]
                if step["light"].get("torch"):
                    cfg["torch"] = True
                _zone_auto_start(state, cfg)
                fired["zones_auto"] = cfg["mode"]
            else:
                _zone_auto_cancel(state)
        # 自動進行: この step の終わり + gap に次を予約(音/映像を出した step のみ)
        if step.get("url") or step.get("video"):
            nxt = _auto_schedule(state, at, step, i)
        else:
            _auto_cancel(state)
            nxt = None
        fired["next_at"] = nxt
        fired["auto"] = bool(state.get("show_auto"))
        _save_state()
        return {"ok": True, **fired}

    if touched:
        _save_state()
        return {"ok": True, "auto": state["show_auto"], "gap": state["show_gap"],
                "next_at": state.get("show_next_at")}

    raise BadRequest('{"steps":[...]} | {"next":true} | {"goto":n} | {"reset":true} | {"auto":bool,"gap":s}')


async def api_show(request):
    """ショーランナー: 一晩のセットリストを組み、NEXTボタンだけで進行する。
    各ステップ = 音(url) + 映像(video) + ライト(light) の束を1つの同期瞬間として発火。
    body: {steps:[{label,url?,video?,gain?,loop?,lead?|tc?,light:{pattern,colors,bpm,...}?}]}
        | {next:true} | {goto:<1始まりの番号>} | {reset:true}
    GET: 現在のセットリスト(プレビュー用・読み取りのみ、秘密を含まないので公開)。"""
    if request.method == "GET":
        state = _chan(request.query.get("ch", "festival"))
        show = state.get("show") or []

        def _ver(u):
            """/assets/x の更新時刻(プレビューのキャッシュバスター用)。"""
            if not isinstance(u, str) or not u.startswith("/assets/"):
                return 0
            try:
                return int(os.path.getmtime(os.path.join(ASSETS, os.path.basename(u))))
            except OSError:
                return 0

        steps = [dict(s, _v=max(_ver(s.get("url")), _ver(s.get("video")))) for s in show]
        return web.json_response({
            "show": {"i": state.get("show_i", -1) + 1, "total": len(show),
                     "auto": bool(state.get("show_auto")), "steps": steps},
            "steps": steps,
            "trailer": {"h": _ver("/assets/dj42-trailer.mp4"),
                        "v": _ver("/assets/dj42-trailer-vertical.mp4")},
        })
    state = _admin_state(request)
    try:
        return web.json_response(await do_show(state, await request.json()))
    except BadRequest as e:
        raise web.HTTPBadRequest(text=str(e))


DJ42_KEY = os.environ.get("SOLUNA_DJ42_KEY", "dj42")


async def api_dj42(request):
    """キー(dj42)だけで6曲を全端末へ配信する簡易リモコン。
    音・光・映像をそれぞれ on/off できる(既定: 音ON・光ON・映像OFF)。
    本体は do_show。body:
      {key, action:"play", i, audio?, light?, video?, lead?} | {action:"stop"} | {action:"next"}
    認証は admin ではなく専用キー(会場スタッフがスマホから叩けるように)。"""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="json body required")
    if (body.get("key") or "").strip().lower() != DJ42_KEY.lower():
        raise web.HTTPForbidden(text="bad key")
    state = _chan(body.get("ch") or "festival")
    action = body.get("action") or "play"
    # 音・光・映像の on/off(既定: 音ON・光ON・映像OFF)
    state["show_no_audio"] = not body.get("audio", True)
    state["show_no_light"] = not body.get("light", True)
    state["show_no_video"] = not body.get("video", False)
    try:
        if action == "stop":
            await do_cue(state, {"stop": True})
            await do_light(state, {"stop": True})
            return web.json_response({"ok": True, "stopped": True,
                                      "listeners": len(state["listeners"])})
        if action == "next":
            r = await do_show(state, {"next": True})
            return web.json_response({"ok": True, **r, "listeners": len(state["listeners"])})
        # ライトだけ即時に変える(曲は止めない)。DJブースのクイックルック用
        if action == "light":
            look = {k: body[k] for k in ("pattern", "colors", "bpm", "brightness", "speed")
                    if body.get(k) is not None}
            r = await do_light(state, look)
            return web.json_response({"ok": True, **r, "listeners": len(state["listeners"])})
        if action == "light_stop":
            r = await do_light(state, {"stop": True, "fade_ms": body.get("fade_ms", 800)})
            return web.json_response({"ok": True, **r, "listeners": len(state["listeners"])})
        # ミュート(キルスイッチ): 端末は止めずゲイン0。解除で位相ズレなく戻る
        if action == "mute":
            state["mute"] = bool(body.get("on", True))
            _save_state()
            await _broadcast_text(state, json.dumps({"t": "mute", "on": state["mute"]}))
            return web.json_response({"ok": True, "mute": state["mute"],
                                      "listeners": len(state["listeners"])})
        # play: 指定トラックを発火(0始まり)
        show = state.get("show") or []
        i = int(body.get("i", 0))
        if i < 0 or i >= len(show):
            raise web.HTTPBadRequest(text="index out of range")
        step = show[i]
        # ステム(方式C): 事前に分けたパートだけを流す。url を選んだステムへ差し替える。
        # 差し替えはこのリクエスト内だけで、state.json には残さない。
        stem = (body.get("stem") or "").strip().lower()
        saved = None
        if stem and stem != "full":
            stems = step.get("stems") or {}
            # 方式C拡張: カンマ区切りで複数パートを同時再生(drums,bass 等)。
            parts = [p for p in (x.strip() for x in stem.split(",")) if p]
            bad = next((p for p in parts if p not in stems), None)
            if bad is not None:
                raise web.HTTPBadRequest(text=f"unknown stem: {bad}")
            saved = dict(step)
            urls = [stems[p] for p in parts]
            step["url"] = urls[0]
            step["urls"] = urls              # 複数パート同時再生(端末が一斉に鳴らす)
            step.pop("video", None)          # ステム再生では映像は送らない
        old_lead = step.get("lead")
        step["lead"] = float(body.get("lead", 0.6))   # タップ後すぐ鳴らす(全端末を揃える最小リード)
        state["show_i"] = i - 1              # 次の next で i 番を発火
        try:
            r = await do_show(state, {"next": True})
        finally:
            if old_lead is None:
                step.pop("lead", None)
            else:
                step["lead"] = old_lead
            if saved is not None:
                step.clear(); step.update(saved)
                _save_state()                # 差し替えを永続化しない
        return web.json_response({"ok": True, **r, "stem": stem or "full",
                                  "listeners": len(state["listeners"])})
    except BadRequest as e:
        raise web.HTTPBadRequest(text=str(e))


async def flags_page(request):
    """A4印刷用のゾーン旗: 大きなゾーン文字+そのゾーン直行のQR。1ゾーン=1ページ。?gate=1 で入場QR。"""
    ch = request.query.get("ch", "festival")
    base = request.query.get("base") or _public_base(request)
    zones = list(_chan(ch)["zones"].keys())
    try:
        import io
        import qrcode
        import qrcode.image.svg

        def _qr_svg(url):
            img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                              box_size=18, border=2)
            buf = io.BytesIO()
            img.save(buf)
            svg = buf.getvalue().decode()
            return svg.replace("<svg", '<svg class="qr"', 1)
        qr_svg = _qr_svg
    except Exception:
        qr_svg = None

    def _qr_block(url):
        inner = qr_svg(url) if qr_svg else f'<div class="url">{url}</div>'
        return f'<div class="qrwrap">{inner}</div>'

    gate = request.query.get("gate") == "1"
    pages = []
    if gate:
        # 入場QR: ゾーン無し(GPS自動/後で選ぶ)・開いた瞬間に音源を先読み → 開演時のDLバーストをゼロに
        url = f"{base}/?gate=1" + (f"&ch={ch}" if ch != "festival" else "")
        pages.append(f'''<section class="flag" data-gate="1">
  <div class="brand"><span class="mark"></span>SOLUNA · SOUND</div>
  <div class="gatehead"><span class="ja">入場</span><span class="en">ENTRANCE</span></div>
  <div class="say">いま読み取ると、中に入る前に音の準備が終わります。<br><span class="en">Scan now — your phone gets the sound ready before you're inside.</span></div>
  {_qr_block(url)}
  <div class="url">{url}</div>
  <div class="foot">スマホでよみとる → あとは会場で ▶ を押すだけ · 位置情報は端末の中だけ</div>
</section>''')
        zones = []
    for z in zones:
        url = f"{base}/?zone={z}" + (f"&ch={ch}" if ch != "festival" else "")
        pages.append(f'''<section class="flag">
  <div class="brand"><span class="mark"></span>SOLUNA · SOUND</div>
  <div class="zonelabel">ZONE · ゾーン</div>
  <div class="letter">{z}</div>
  <div class="say">この旗の近くなら、ここを読み取って ▶ 。<br><span class="en">Near this flag? Scan &amp; tap ▶ — you become the sound.</span></div>
  {_qr_block(url)}
  <div class="url">{url}</div>
  <div class="foot">音が出ない時: サイレントスイッチ OFF・音量アップ · No sound? Silent switch off, volume up</div>
</section>''')

    n = len(pages)
    title = "ENTRANCE QR" if gate else "ZONE FLAGS"
    html = f'''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOLUNA {title.lower()}</title><style>
  :root{{--ink:#0a0507;--gold:#d4af37;--gold2:#f0d47a;--cream:#f4e8d0;--dim:#a8977e;--moon:#c8d3e6}}
  *{{margin:0;box-sizing:border-box}}
  body{{font-family:"Zen Kaku Gothic New","Hiragino Sans",system-ui,sans-serif;background:#1a1216;color:var(--cream)}}
  .flag{{width:210mm;height:297mm;padding:14mm 16mm;display:flex;flex-direction:column;align-items:center;justify-content:space-between;
        background:radial-gradient(70% 45% at 50% -10%,rgba(212,175,55,.22),transparent 70%),var(--ink);color:var(--cream);position:relative;overflow:hidden}}
  .flag::after{{content:"";position:absolute;inset:6mm;border:0.4mm solid rgba(212,175,55,.35);border-radius:6mm;pointer-events:none}}
  .brand{{display:flex;align-items:center;gap:4mm;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:6.5mm;letter-spacing:2mm;color:var(--gold);font-weight:700}}
  .mark{{width:9mm;height:9mm;border-radius:50%;background:conic-gradient(from 200deg,var(--gold) 0 50%,var(--moon) 50% 100%);box-shadow:0 0 8mm rgba(212,175,55,.45)}}
  .zonelabel{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:5mm;letter-spacing:2mm;color:var(--dim);margin-top:2mm}}
  .letter{{font-size:118mm;font-weight:700;line-height:.95;color:var(--gold);font-family:"Shippori Mincho","Hiragino Mincho ProN",Georgia,serif;
          text-shadow:0 0 14mm rgba(212,175,55,.35);margin-top:-6mm}}
  .gatehead{{display:flex;flex-direction:column;align-items:center;line-height:1;margin-top:6mm}}
  .gatehead .ja{{font-size:62mm;font-weight:700;color:var(--gold);font-family:"Shippori Mincho","Hiragino Mincho ProN",serif;text-shadow:0 0 14mm rgba(212,175,55,.35)}}
  .gatehead .en{{font-size:18mm;letter-spacing:6mm;color:var(--cream);font-family:"IBM Plex Mono",ui-monospace,monospace;margin-top:4mm;text-indent:6mm}}
  .say{{font-size:7.2mm;text-align:center;line-height:1.7;color:var(--cream);font-weight:500}}
  .say .en{{font-size:5.2mm;color:var(--dim);font-weight:400}}
  .flag[data-gate] .say{{font-size:6.2mm}}
  .qrwrap{{background:#fff;padding:5mm;border-radius:5mm;box-shadow:0 6mm 18mm rgba(0,0,0,.45)}}
  .qr{{width:84mm;height:84mm;display:block}}
  .url{{font-size:4.2mm;color:var(--dim);font-family:"IBM Plex Mono",ui-monospace,monospace;word-break:break-all;text-align:center}}
  .foot{{font-size:3.8mm;color:var(--dim);text-align:center;line-height:1.6}}
  @media screen{{
    body{{padding:24px 12px 60px}}
    .toolbar{{max-width:900px;margin:0 auto 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;color:var(--dim);font-size:.85rem}}
    .toolbar b{{color:var(--gold);font-family:"IBM Plex Mono",ui-monospace,monospace;letter-spacing:.2em}}
    .toolbar button{{background:linear-gradient(180deg,var(--gold2),var(--gold));color:#1a1206;border:0;border-radius:12px;padding:10px 18px;font-weight:700;font-size:.95rem;cursor:pointer}}
    .sheet{{display:flex;flex-wrap:wrap;gap:20px;justify-content:center}}
    .slot{{width:calc(210mm * .5);height:calc(297mm * .5);overflow:hidden;border-radius:10px;box-shadow:0 20px 50px rgba(0,0,0,.5)}}
    .slot .flag{{transform:scale(.5);transform-origin:top left}}
  }}
  @media print{{
    body{{background:#fff}} .toolbar{{display:none}} .sheet{{display:block}} .slot{{width:auto;height:auto;overflow:visible;box-shadow:none;border-radius:0}}
    .flag{{page-break-after:always;break-after:page;transform:none;margin:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
    @page{{size:A4 portrait;margin:0}}
  }}
</style></head><body>
<div class="toolbar"><div><b>SOLUNA · {title}</b> &nbsp; {n} <span class="tb-help">枚 · A4 縦 · 印刷ダイアログで「背景のグラフィック」をON</span></div>
  <button onclick="window.print()">🖨 印刷 / Print</button></div>
<div class="sheet">{"".join(f'<div class="slot">{pg}</div>' for pg in pages)}</div>
<script src="/ui/i18n.js"></script>
</body></html>'''
    return web.Response(text=html, content_type="text/html")


def _qr_svg_dataurl(url, box=8, border=2):
    """URLをQRのSVG data URLにする(依存が無ければNone)。"""
    try:
        import io
        import base64
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=box, border=border)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode()
        b64 = base64.b64encode(svg.encode()).decode()
        return f"data:image/svg+xml;base64,{b64}"
    except Exception:
        return None


async def api_qr(request):
    """観客が友達にシェアする用のQR(SVG)を返す。
    URLはサーバが自前で組み立てる(ゾーン/チャンネルのみ受ける)ので任意URLの
    QR生成には使えない。認証不要=参加者全員が自分の画面で出せる。"""
    ch = request.query.get("ch", "festival")
    zone = (request.query.get("zone") or "").upper()
    gate = request.query.get("gate") == "1"
    base = _public_base(request)
    q = []
    if gate:
        q.append("gate=1")
    if ch and ch != "festival":
        q.append("ch=" + quote(ch))
    if zone and zone in _chan(ch)["zones"]:
        q.append("zone=" + quote(zone))
    url = base + "/" + ("?" + "&".join(q) if q else "")
    try:
        import io
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=300",
                                     "X-QR-URL": url})
    except Exception as e:
        raise web.HTTPServiceUnavailable(text=f"qr unavailable: {e}")


async def guide_page(request):
    """全URL・使い方・QR・本番手順を1枚にまとめた案内ページ。
    トークン付きURLのQRは管理トークンで開いた時だけ出す(?token=… or ヘッダ)。"""
    base = request.query.get("base") or _public_base(request)
    ch = request.query.get("ch", "festival")
    zones = list(_chan(ch)["zones"].keys())

    # トークンは本人がURLで渡した時だけ使う(公開ページに漏らさない)
    qtok = request.query.get("token") or ""
    is_admin = _admin_ok(request) or (bool(os.environ.get("SOLUNA_ADMIN")) and qtok == os.environ.get("SOLUNA_ADMIN"))
    dj_tok = os.environ.get("SOLUNA_DJ_TOKEN") or ""
    admin_tok = os.environ.get("SOLUNA_ADMIN") or ""

    def qr(url, label, sub="", cls=""):
        src = _qr_svg_dataurl(url)
        img = (f'<img class="qr" src="{src}" alt="{label}のQR">' if src
               else f'<div class="qr-fallback">{url}</div>')
        return f'''<div class="qcard {cls}">
  <div class="qtop"><b>{label}</b>{f'<span class="qsub">{sub}</span>' if sub else ''}</div>
  {img}
  <a class="qurl" href="{url}" target="_blank" rel="noopener">{url}</a>
</div>'''

    join_url = f"{base}/"
    gate_url = f"{base}/?gate=1" + (f"&ch={ch}" if ch != "festival" else "")
    screen_url = f"{base}/screen"
    dj_url = f"{base}/dj"
    admin_url = f"{base}/admin"
    connect_url = f"{base}/connect"
    about_url = f"{base}/about"
    flags_url = f"{base}/flags" + (f"?ch={ch}" if ch != "festival" else "")
    gateflags_url = f"{base}/flags?gate=1" + (f"&ch={ch}" if ch != "festival" else "")

    zone_cards = "".join(
        qr(f"{base}/?zone={z}" + (f"&ch={ch}" if ch != "festival" else ""),
           f"ゾーン {z}", "この旗の近くで読み取る") for z in zones)

    # 秘密トークン付きURL(管理トークンで開いた時だけ表示)
    secret_block = ""
    if is_admin:
        dj_secret = f"{dj_url}?token={dj_tok}" if dj_tok else ""
        admin_secret = f"{admin_url}?token={admin_tok}" if admin_tok else ""
        rows = []
        if dj_secret:
            rows.append(qr(dj_secret, "DJ 配信元(キー付き)", "DJ本人だけに渡す", "secret"))
        if admin_secret:
            rows.append(qr(admin_secret, "管理画面(キー付き)", "主催者・音響だけに渡す", "secret"))
        if rows:
            secret_block = f'''
<section class="blk">
  <h2>🔑 キー付きURL<span class="hint">このページを管理トークンで開いた時だけ表示・本人以外に共有しない</span></h2>
  <div class="qgrid">{"".join(rows)}</div>
</section>'''

    html = f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>SOLUNA Sound — URL・使い方まとめ</title>
<meta name="description" content="SOLUNA Sound の全URL・QR・使い方・本番手順を1枚に。観客・DJ・FOH・投影・接続ガイド。">
<meta name="robots" content="noindex">
<link rel="icon" href="/favicon.ico"><link rel="manifest" href="/manifest.webmanifest">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap">
<link rel="stylesheet" href="/ui/soluna.css">
<style>
  .hero{{padding:44px 0 10px;text-align:center}}
  .hero h1{{font-family:var(--serif);font-size:clamp(1.8rem,5.5vw,2.8rem);line-height:1.2;margin:14px 0 10px;text-wrap:balance}}
  .hero p{{color:var(--dim);font-size:1rem;max-width:36em;margin:0 auto}}
  section.blk{{margin-top:48px}}
  section.blk>h2{{font-family:var(--mono);font-size:.72rem;letter-spacing:.28em;color:var(--gold);margin:0 0 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
  section.blk>h2::after{{content:"";flex:1;height:1px;background:var(--line)}}
  section.blk>h2 .hint{{letter-spacing:0;font-family:var(--sans);color:var(--dim);font-size:.72rem;font-weight:400}}
  .qgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
  .qcard{{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}}
  .qcard.secret{{border-color:rgba(212,175,55,.55);box-shadow:0 0 0 3px rgba(212,175,55,.12)}}
  .qtop{{display:flex;flex-direction:column;gap:2px}}
  .qtop b{{font-size:.95rem;color:var(--txt)}}
  .qsub{{font-size:.7rem;color:var(--dim)}}
  .qr{{width:150px;height:150px;background:#fff;border-radius:10px;padding:8px;display:block}}
  .qr-fallback{{font-family:var(--mono);font-size:.7rem;color:var(--dim);word-break:break-all;background:#0e0709;border:1px dashed var(--line2);border-radius:10px;padding:12px;width:150px;min-height:150px;display:grid;place-items:center}}
  .qurl{{font-family:var(--mono);font-size:.68rem;color:var(--gold2);word-break:break-all}}
  .steps{{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}}
  .step{{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-lg);padding:16px}}
  .step b.k{{display:block;font-family:var(--serif);font-size:1.5rem;color:var(--gold);line-height:1;margin-bottom:8px}}
  .step h3{{margin:0 0 6px;font-size:1rem}}
  .step p{{margin:0;color:var(--dim);font-size:.88rem;line-height:1.6}}
  .step ol{{margin:6px 0 0;padding-left:1.2em;color:var(--dim);font-size:.85rem;line-height:1.7}}
  table.s-table{{width:100%;border-collapse:collapse;font-size:.86rem}}
  table.s-table td,table.s-table th{{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left;vertical-align:top}}
  table.s-table th{{color:var(--dim);font-weight:500;font-size:.72rem;letter-spacing:.1em}}
  table.s-table td:first-child{{color:var(--gold2);white-space:nowrap;font-family:var(--mono);font-size:.8rem}}
  .note{{border-left:2px solid var(--gold);padding-left:12px;color:var(--dim);font-size:.85rem;margin-top:12px;line-height:1.7}}
  .note b{{color:var(--txt)}}
  .setlist{{display:flex;flex-wrap:wrap;gap:8px}}
  .setlist span{{font-family:var(--mono);font-size:.78rem;color:var(--txt);background:var(--panel2);border:1px solid var(--line2);border-radius:999px;padding:5px 12px}}
  footer{{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);font-size:.78rem;color:var(--dim);display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between}}
  @media print{{ body{{background:#fff;color:#000}} .qr{{width:120px;height:120px}} section.blk{{page-break-inside:avoid}} }}
</style></head>
<body>
<div class="s-wrap">
  <div class="s-bar"><div class="s-brand"><span class="mark"></span>SOLUNA · SOUND <small>URL &amp; GUIDE</small></div>
    <div style="margin-left:auto;display:flex;gap:8px"><a class="s-btn ghost" href="{about_url}">紹介</a><a class="s-btn ghost" href="{connect_url}">接続ガイド</a></div></div>

  <div class="hero">
    <h1>全部のURLと、使い方。</h1>
    <p>会場ごと、ひとつの時計で鳴る。観客は読み取って▶、DJはON AIR、主催者はFOH。下のQRからそのまま開けます。</p>
  </div>

  <section class="blk">
    <h2>🎧 参加・視聴<span class="hint">来場者に配るQR</span></h2>
    <div class="qgrid">
      {qr(join_url, "観客(参加)", "スマホで開いて ▶")}
      {qr(gate_url, "入場QR", "並んでいる間に音源を先読み")}
      {qr(screen_url, "全画面投影", "プロジェクター/LEDに")}
    </div>
  </section>

  <section class="blk">
    <h2>📍 ゾーン別QR<span class="hint">旗に印刷して客席に置く</span></h2>
    <div class="qgrid">{zone_cards}</div>
    <div class="note">印刷は <a href="{flags_url}">/flags</a>(ゾーン旗) と <a href="{gateflags_url}">/flags?gate=1</a>(入場QR) からA4で。印刷ダイアログで「背景のグラフィック」をON。</div>
  </section>

  <section class="blk">
    <h2>🎚 運営<span class="hint">DJ・主催者・音響</span></h2>
    <div class="qgrid">
      {qr(dj_url, "DJ 配信元", "トークンはDJ本人から")}
      {qr(admin_url, "管理画面(FOH)", "トークンは主催者から")}
      {qr(connect_url, "接続ガイド", "Dante/AES67/OSC/灯体")}
    </div>
  </section>
  {secret_block}

  <section class="blk">
    <h2>▶ 使い方<span class="hint">役割ごとに3ステップ</span></h2>
    <div class="steps">
      <div class="step"><b class="k">観客</b><h3>読み取って ▶ だけ</h3>
        <ol><li>QRか {base} を開く</li><li>ゾーンを選ぶ(旗の近くなら自動)</li><li>「▶ 参加する」でその端末がスピーカーに</li><li>画面の色はマイクの音に反応(使えなければ会場と同期)</li></ol></div>
      <div class="step"><b class="k">DJ</b><h3>ON AIR で会場へ</h3>
        <ol><li>DJトークン付きURLを開く(自動保存)</li><li>マイク/ライン/ファイルを選ぶ</li><li>ON AIR。MUTEは即停止</li></ol></div>
      <div class="step"><b class="k">主催者</b><h3>曲を入れてAUTO</h3>
        <ol><li>管理トークンで /admin</li><li>曲アップ→セットリスト→光の見た目</li><li>AUTOをON。NEXTで次へ</li></ol></div>
    </div>
  </section>

  <section class="blk">
    <h2>🎪 本番の流れ<span class="hint">FOH担当の当日手順</span></h2>
    <div class="steps">
      <div class="step"><b class="k">1</b><h3>準備</h3><p>曲アップ→セットリスト→各曲に光。Pi箱を客席15m格子で配置・電源ON(1分で自動集合)。</p></div>
      <div class="step"><b class="k">2</b><h3>音を入れる</h3><p>既存PAに足すなら卓のaux/matrix→USB I/F→<span class="mono">source.py --input</span>。ゾーンを距離で設定。</p></div>
      <div class="step"><b class="k">3</b><h3>位相合わせ</h3><p>クリックを流し、ALIGNの±msでフラム(二重打ち)が消えるまで追い込む。ここが品質の要。</p></div>
      <div class="step"><b class="k">4</b><h3>本番</h3><p>AUTOをON→夜が自動進行。NEXTで次へ、MUTEで停止。1台死んでも15〜20秒で継続。</p></div>
    </div>
    <div class="note"><b>正直な注記:</b> 屋外GPS実精度・実iPhone多台数の一晩・実Dante/LTC/照明卓/灯体は合成信号でのみ検証。大規模本番前に会場でリハーサル(ALIGN含む)を1回推奨。</div>
  </section>

  <section class="blk">
    <h2>📊 数字<span class="hint">measured, not promised</span></h2>
    <table class="s-table">
      <tr><th>項目</th><th>値</th></tr>
      <tr><td>同期精度</td><td>LAN ±1〜3ms / LTE ±10〜20ms / Pi箱内 ±0.5ms</td></tr>
      <tr><td>自己復旧</td><td>サーバ強制終了から6秒 / 1台停止から15〜20秒で継続</td></tr>
      <tr><td>同時接続</td><td>LAN単一プロセスで10,000台(到達100%・開始一致) / Pi 4 1台で5,000台</td></tr>
      <tr><td>本番帯域</td><td>音は先に配る。鳴らす合図だけ配るので5,000台でも増えない</td></tr>
    </table>
  </section>

  <section class="blk">
    <h2>🎵 セットリスト<span class="hint">現在 6曲</span></h2>
    <div class="setlist"><span>Aetheria</span><span>40Hz VIP Sovereignty</span><span>Emotional Gravity</span><span>We Got the AURA</span><span>Quantum Afro-Amapiano</span><span>PRADA (Mainstage Techno Overdrive)</span></div>
  </section>

  <footer><span>SOLUNA Sound · MIT · github.com/yukihamada/soluna-surround</span><span>位置情報は端末の中だけで使い、送信しません。</span></footer>
</div>
</body></html>'''
    return web.Response(text=html, content_type="text/html")


async def api_geo(request):
    """ステージ位置(緯度経度)を登録 → 端末はGPS距離からゾーンを自動選択できる。
    body: {lat, lng} or {clear:true}。FOHが舞台前で「現在地を登録」する運用。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch", "festival")
    state = _chan(name)
    body = await request.json()
    if body.get("clear"):
        state["geo"] = None
    else:
        try:
            state["geo"] = {"lat": float(body["lat"]), "lng": float(body["lng"])}
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text='{"lat":21.33,"lng":-158.08} or {"clear":true}')
    _save_state()
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "geo": state["geo"],
                              "listeners": len(state["listeners"])})


async def do_align(state, base_ms: float) -> dict:
    state["base_ms"] = float(base_ms)
    _save_state()
    await _broadcast_text(state, _config_msg(state))
    return {"ok": True, "base_ms": state["base_ms"], "listeners": len(state["listeners"])}


async def api_align(request):
    """既存ハウスPAとの位相合わせ: 全ゾーン一括トリム(ms, 負値=早める方向)。
    サウンドチェックでクリックをハウスPA+グリッド同時に鳴らし、フラム感が
    消えるまで ±5ms 刻みで追い込む。"""
    state = _admin_state(request)
    body = await request.json()
    return web.json_response(await do_align(state, body.get("base_ms", 0.0)))


def do_timecode(state, tc: str, fps: float = 30, drop=None, epoch=None) -> dict:
    """「今(epoch)この瞬間の番組タイムコードは tc」を基準として保存。以後 cue の
    {"tc":"01:00:10:00"} が epoch に変換できる。LTC(ltc.py)や OSC /soluna/tc から毎秒来てよい
    (毎回上書き=再生卓のドリフトに追従)。"""
    anchor = showctl.tc_anchor(str(tc), float(fps or 30), epoch if epoch is not None else now(), drop)
    state["tc"] = anchor
    _save_state()
    return {"ok": True, "tc": anchor}


async def api_timecode(request):
    """POST {"tc":"01:00:00:00","fps":30} (;FF または fps=29.97 でドロップフレーム)。
    GET → 現在の基準と、いまの番組タイムコード(推定)。"""
    state = _admin_state(request)
    if request.method == "GET":
        anchor = state.get("tc")
        cur = None
        if anchor:
            frames = anchor["frames"] + int((now() - anchor["epoch"])
                                            * showctl.tc_rate(anchor["fps"], anchor.get("drop"))[0])
            cur = showctl.frames_to_tc(frames, anchor["fps"], anchor.get("drop"))
        return web.json_response({"ok": True, "tc": anchor, "now_tc": cur})
    body = await request.json()
    try:
        return web.json_response(do_timecode(state, body.get("tc"), body.get("fps", 30),
                                             body.get("drop"), body.get("epoch")))
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=f'{{"tc":"HH:MM:SS:FF","fps":30}} — {e}')


def _track_peaks(state):
    """事後レポート用のピーク更新(接続時・レポート受信時・statusで呼ぶ。安価)。"""
    st = state["stats"]
    st["started"] = st["started"] or now()
    n = len(state["listeners"])
    if n > st["peak_listeners"]:
        st["peak_listeners"] = n
    playing = 0
    by_zone = {}
    for meta in state["listeners"].values():
        rep_ = meta.get("rep") or {}
        if rep_.get("st") == "playing":
            playing += 1
            z = meta.get("zone") or "?"
            by_zone[z] = by_zone.get(z, 0) + 1
    if playing > st["peak_playing"]:
        st["peak_playing"] = playing
    lit = 0
    for meta in state["listeners"].values():
        rep_ = meta.get("rep") or {}
        if rep_.get("lit"):
            lit += 1
    if lit > st.get("peak_lit", 0):
        st["peak_lit"] = lit
    for z, c in by_zone.items():
        if c > st["peak_playing_zone"].get(z, 0):
            st["peak_playing_zone"][z] = c


async def api_mute(request):
    """FOHキルスイッチ: {"on":true|false}。CUEもLIVEも即時無音(端末は再生を止めずゲインを0に=解除で位相ズレなし)。"""
    state = _admin_state(request)
    body = await request.json()
    state["mute"] = bool(body.get("on", True))
    _save_state()
    await _broadcast_text(state, json.dumps({"t": "mute", "on": state["mute"]}))
    return web.json_response({"ok": True, "mute": state["mute"], "listeners": len(state["listeners"])})


async def api_stats(request):
    """事後レポート(admin): ピーク接続/再生数・ゾーン別ピーク・cue数・稼働時間。reset=1 で新しい公演へ。"""
    state = _admin_state(request)
    _track_peaks(state)
    if request.query.get("reset") == "1":
        state["stats"] = {"peak_listeners": 0, "peak_playing": 0, "peak_playing_zone": {}, "cues": 0,
                          "lights": 0, "peak_lit": 0, "first_cue": None, "last_cue": None, "started": now()}
        _save_state()
    st = dict(state["stats"])
    st["uptime_s"] = round(now() - (st["started"] or now()), 1)
    st["now_listeners"] = len(state["listeners"])
    st["nodes"] = sum(1 for m in state["listeners"].values() if (m.get("rep") or {}).get("kind") == "node")
    return web.json_response(st)


async def api_preload(request):
    """入場QR用(認証なし・極小): いま端末が先読みすべき音源URL。WSを張らずにDLだけ済ませる。
    無線対策の要=開演時のDLバーストを入口に分散させる。"""
    state = _chan(request.query.get("ch", "festival"))
    url = state.get("preload") or (state["cue"] or {}).get("url")
    video = (state["cue"] or {}).get("video")
    return web.json_response({"url": url, "video": video, "asset_base": ASSET_BASE or None},
                             headers={"Cache-Control": "public, max-age=10"})


async def api_net(request):
    """回線案内: {"ssid":"SOLUNA-Front","wifi_zones":["A","B"]} → 前方ゾーンの端末に
    「会場Wi-Fiへ」、それ以外に「モバイル回線のままでOK」を表示。null で消す。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    state = _chan(request.query.get("ch", "festival"))
    body = await request.json()
    if not body or body.get("clear"):
        state["net"] = None
    else:
        zones = body.get("wifi_zones") or []
        if not isinstance(zones, list):
            raise web.HTTPBadRequest(text='{"ssid":"...","wifi_zones":["A","B"]}')
        state["net"] = {"ssid": str(body.get("ssid") or "")[:64],
                        "wifi_zones": [str(z).upper() for z in zones][:26]}
    _save_state()
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "net": state["net"]})


async def api_zones(request):
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch", "festival")
    state = _chan(name)
    body = await request.json()
    zones = body.get("zones")
    zones_m = body.get("zones_m")            # 距離[m]指定 → delay自動計算
    if isinstance(zones_m, dict) and zones_m:
        zones = {k: float(v) / 343.0 * 1000.0 + 15.0 for k, v in zones_m.items()}
    if (not isinstance(zones, dict) or not zones) and isinstance(body.get("gains_db"), dict):
        zones = state["zones"]                # 音量補正だけ更新
    if not isinstance(zones, dict) or not zones:
        raise web.HTTPBadRequest(
            text='{"zones":{"A":15.0,...}} (delay_ms) or {"zones_m":{"A":15,...}} (距離m)')
    state["zones"] = {str(k).upper(): round(float(v), 1) for k, v in zones.items()}
    gains = body.get("gains_db")             # ゾーン別音量補正[dB] (±12にクリップ)
    if isinstance(gains, dict):
        state["zone_gain_db"] = {str(k).upper(): round(max(-12.0, min(12.0, float(v))), 1)
                                 for k, v in gains.items()}
    _save_state()
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "zones": state["zones"],
                              "zone_gain_db": state["zone_gain_db"]})


async def api_upload(request):
    """FOHの音源/映像アップロード → /assets/<name> で配信。body=生バイト。
    注意: コンテナFSなので再デプロイで消える(本番当日は開演前に上げ直す)。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = os.path.basename(request.query.get("name", "").strip())
    if not name or name.startswith("."):
        raise web.HTTPBadRequest(text="?name=<filename> required")
    data = await request.read()
    if not data:
        raise web.HTTPBadRequest(text="empty body")
    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, name), "wb") as f:
        f.write(data)
    return web.json_response({"ok": True, "url": f"/assets/{name}", "bytes": len(data)})


async def api_assets(request):
    """assets/ 配下の音源一覧(キューコンソールのファイルピッカー用)。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    files = []
    if os.path.isdir(ASSETS):
        for f in sorted(os.listdir(ASSETS)):
            p = os.path.join(ASSETS, f)
            if os.path.isfile(p) and not f.startswith("."):
                files.append({"name": f, "url": f"/assets/{f}",
                              "bytes": os.path.getsize(p)})
    return web.json_response({"ok": True, "assets": files})


def _devices_summary(st):
    """端末レポートの集計: FOHが「何台が実際に鳴っているか」を見るため。
    レポート無し(旧client/play.py)は unknown に数える。"""
    agg = {"preloaded": 0, "playing": 0, "idle": 0, "failed": 0, "unknown": 0,
           "low_battery": 0, "ctx_suspended": 0, "stale": 0, "nodes": 0, "lit": 0}
    by_zone_playing = {}
    t = now()
    for meta in st["listeners"].values():
        rep = meta.get("rep")
        if not rep:
            agg["unknown"] += 1
            continue
        if rep.get("kind") == "node":
            agg["nodes"] += 1
        stt = rep.get("st") or "idle"
        agg[stt if stt in agg else "unknown"] += 1
        if rep.get("lit"):
            agg["lit"] += 1
        if stt == "playing":
            z = meta.get("zone") or "?"
            by_zone_playing[z] = by_zone_playing.get(z, 0) + 1
        if rep.get("bat") is not None and rep["bat"] < 0.2:
            agg["low_battery"] += 1
        if rep.get("ctx") and rep["ctx"] != "running":
            agg["ctx_suspended"] += 1
        if t - rep.get("t", t) > 60:
            agg["stale"] += 1
    agg["by_zone_playing"] = by_zone_playing
    return agg


async def health(request):
    n = sum(len(st["listeners"]) for st in channels.values())
    t = now()
    return web.json_response({"ok": True, "version": VERSION, "role": "server",
                              "uptime_s": round(time.time() - STARTED_AT, 1),
                              "listeners": n, "channels": len(channels),
                              "nodes": sum(1 for v in nodes.values() if t - v["t"] <= NODE_STALE_S),
                              "data_dir": DATA_DIR, "asset_base": ASSET_BASE or None,
                              "server_epoch_ms": now() * 1000.0})


async def api_state(request):
    """ショー状態の丸ごと書き出し/読み込み(ホットスタンバイ切替用)。
    GET → 永続分(zones/align/geo/cue/light/show…)を JSON で返す。
    POST {channels:{...}} → 上書きして全端末へ config を再配信。
    運用: 予備FOH Macに定期 GET で控えを取り、主機が死んだら予備で POST → 端末は
    再接続先(同ホスト名/IP引継ぎ)で同じキューを受け取り曲中復帰する。
    2台のMacがNTPで揃っていれば cue.at(epoch秒)はそのまま有効。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    if request.method == "GET":
        out = {name: {k: st.get(k) for k in _PERSIST} for name, st in channels.items()}
        return web.json_response({"ok": True, "version": VERSION,
                                  "exported_at": now(), "channels": out})
    body = await request.json()
    chans = body.get("channels")
    if not isinstance(chans, dict):
        raise web.HTTPBadRequest(text='{"channels":{"festival":{...}}} required')
    for name, vals in chans.items():
        st = _chan(str(name))
        for k in _PERSIST:
            if k in vals:
                st[k] = vals[k]
        await _broadcast_text(st, _config_msg(st))
        if st.get("cue"):
            await _broadcast_text(st, json.dumps({"t": "cue", **st["cue"]}))
        if st.get("light"):
            await _broadcast_text(st, json.dumps({"t": "light", **st["light"]}))
    _save_state()
    return web.json_response({"ok": True, "imported": list(chans.keys())})


async def api_nodes_report(request):
    """Pi の agent.py が5秒ごとに自分の健康状態を届ける(LAN・無認証・IPごと1req/s)。
    位置情報は無い。psk は AP を立てている箱だけが載せる(/adminだけが読む)。"""
    ip = request.remote or "?"
    t = now()
    if t - _node_rate.get(ip, 0.0) < 0.9:
        raise web.HTTPTooManyRequests(text="1 report/s per ip")
    _node_rate[ip] = t
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="json body required")
    host = str(body.get("host") or "")[:64]
    if not host:
        raise web.HTTPBadRequest(text="host required")
    rec = {k: body.get(k) for k in ("ip", "role", "up_min", "temp", "load", "disk_free_mb",
                                    "audio", "node", "server", "eth", "agent", "ap")}
    rec["ip"] = str(rec.get("ip") or ip)[:45]
    rec["role"] = str(rec.get("role") or "?")[:12]
    rec["host"] = host
    rec["t"] = t
    nodes[host] = rec
    if len(nodes) > 500:                       # 暴走した箱が台帳を膨らませない
        oldest = sorted(nodes.items(), key=lambda kv: kv[1]["t"])[: len(nodes) - 500]
        for k, _ in oldest:
            nodes.pop(k, None)
    return web.json_response({"ok": True, "server_epoch_ms": t * 1000.0})


async def api_nodes(request):
    """/admin NODES: 箱の一覧(stale 付き)+ このチャンネルの割当 + AP 情報。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    state = _chan(request.query.get("ch", "festival"))
    t = now()
    out = []
    ap = None
    cfg = state.get("node_cfg") or {}
    online_hosts = {m.get("host") for st in channels.values() for m in st["listeners"].values() if m.get("host")}
    for host, rec in sorted(nodes.items()):
        r = dict(rec)
        r["age_s"] = round(t - rec["t"], 1)
        r["stale"] = r["age_s"] > NODE_STALE_S
        r["ws"] = host in online_hosts
        r["cfg"] = cfg.get(host) or {}
        if rec.get("role") == "server" and rec.get("ap"):
            ap = rec["ap"]
        r.pop("ap", None)
        out.append(r)
    for host, c in cfg.items():                # 割当だけあって今は見えない箱も出す
        if host not in nodes:
            out.append({"host": host, "stale": True, "age_s": None, "ws": host in online_hosts,
                        "cfg": c, "role": "?", "ip": None})
    return web.json_response({"ok": True, "nodes": out, "stale_s": NODE_STALE_S,
                              "meta": {"ap": ap, "server_host": socket.gethostname()}})


async def api_nodes_assign(request):
    """{"host":"soluna-node-2","zone":"C","pos":"L","gain_db":-3} → 保存 + その箱のWSへ即時 {"t":"assign"}。
    {"host":..,"clear":true} で割当解除。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    state = _chan(request.query.get("ch", "festival"))
    body = await request.json()
    host = str(body.get("host") or "")[:64]
    if not host:
        raise web.HTTPBadRequest(text="host required")
    cfg = state.setdefault("node_cfg", {})
    if body.get("clear"):
        cfg.pop(host, None)
        _save_state()
        return web.json_response({"ok": True, "host": host, "cfg": None, "pushed": 0})
    c = dict(cfg.get(host) or {})
    if body.get("zone") is not None:
        c["zone"] = str(body["zone"]).upper()[:8] or None
    if body.get("pos") is not None:
        pos = str(body["pos"]).upper()[:1]
        if pos not in ("L", "C", "R"):
            raise web.HTTPBadRequest(text="pos must be L|C|R")
        c["pos"] = pos
    if body.get("gain_db") is not None:
        c["gain_db"] = round(max(-24.0, min(24.0, float(body["gain_db"]))), 1)
    cfg[host] = c
    _save_state()
    msg = json.dumps({"t": "assign", **c})
    pushed = 0
    for ws, meta in list(state["listeners"].items()):
        if meta.get("host") == host:
            try:
                await asyncio.wait_for(ws.send_str(msg), timeout=2.0)
                if c.get("zone"):
                    meta["zone"] = c["zone"]
                if c.get("pos"):
                    meta["pos"] = c["pos"]
                pushed += 1
            except Exception:
                pass
    return web.json_response({"ok": True, "host": host, "cfg": c, "pushed": pushed})


def _mdns_publish(port):
    """LAN の Pi/ノードが `_soluna._tcp` で見つけられるように広告(avahi があれば)。クラウドでは無い=無害。"""
    if os.environ.get("SOLUNA_MDNS", "1") != "1" or not shutil.which("avahi-publish-service"):
        return None
    try:
        return subprocess.Popen(["avahi-publish-service", f"SOLUNA {socket.gethostname()}", "_soluna._tcp",
                                 str(port), f"ver={VERSION}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


async def api_channel_delete(request):
    """テスト用チャンネル等をstateから消す(本番の /status を汚さない)。
    接続中のリスナーがいるチャンネルは拒否。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch")
    if not name or name not in channels:
        raise web.HTTPNotFound(text="unknown channel")
    if channels[name]["listeners"] or channels[name]["source"]:
        raise web.HTTPConflict(text="channel has live connections")
    _light_auto_cancel(channels[name])
    _zone_auto_cancel(channels[name])
    _timeline_cancel(channels[name])
    _auto_cancel(channels[name])
    del channels[name]
    _save_state()
    return web.json_response({"ok": True, "deleted": name})


async def status(request):
    out = {}
    for name, st in channels.items():
        zones_n = {}
        for meta in st["listeners"].values():
            key = meta.get("zone") or meta.get("pos") or "?"
            zones_n[key] = zones_n.get(key, 0) + 1
        out[name] = {
            "devices": _devices_summary(st),
            "zone_gain_db": st.get("zone_gain_db") or {},
            "source": st["source"] is not None,
            "level": (st.get("level") if st.get("level") and now() - st["level"]["t"] < 2.0 else None),
            "mute": bool(st.get("mute")),
            "map": st["map"],
            "sr": st["sr"],
            "seq": st["seq"],
            "stream_t": (None if st["epoch"] is None
                         else round(st["played"] / float(st["sr"]), 2)),
            "listeners": len(st["listeners"]),
            "by_zone": zones_n,
            "zones": st["zones"],
            "base_ms": st["base_ms"],
            "live_lead": st["lead"],
            "cue": st["cue"],
            "light": st["light"],
            "light_off_at": st.get("light_off_at"),
            "zones_auto": st.get("zones_auto"),
            "timeline_i": st.get("timeline_i"),
            "presets": sorted((st.get("light_presets") or {}).keys()),
            "geo": st["geo"],
            "tc": st.get("tc"),
            "show": {"i": st.get("show_i", -1) + 1,
                     "total": len(st.get("show") or []),
                     "auto": bool(st.get("show_auto")),
                     "gap": st.get("show_gap", 1.0),
                     "next_at": st.get("show_next_at"),
                     "durs": [x.get("dur") for x in (st.get("show") or [])],
                     "next": ((st.get("show") or [])[st.get("show_i", -1) + 1]["label"]
                              if st.get("show") and st.get("show_i", -1) + 1 < len(st["show"])
                              else None)},
        }
    return web.json_response({"server_epoch_ms": now() * 1000.0, "version": VERSION,
                              "asset_base": ASSET_BASE or None,
                              "lead": LEAD, "cue_lead": CUE_LEAD, "channels": out})


async def index(request):
    return web.FileResponse(os.path.join(HERE, "client.html"))


async def about_page(request):
    return web.FileResponse(os.path.join(HERE, "about.html"))


async def connect_page(request):
    return web.FileResponse(os.path.join(HERE, "connect.html"))


async def production_page(request):
    return web.FileResponse(os.path.join(HERE, "production.html"))


async def admin_page(request):
    return web.FileResponse(os.path.join(HERE, "admin.html"))


async def dj_page(request):
    return web.FileResponse(os.path.join(HERE, "dj.html"))


async def preview_page(request):
    return web.FileResponse(os.path.join(HERE, "preview.html"))


async def dj42_page(request):
    return web.FileResponse(os.path.join(HERE, "dj42.html"))


async def booth_page(request):
    return web.FileResponse(os.path.join(HERE, "booth.html"))


async def favicon(request):
    return web.FileResponse(os.path.join(HERE, "icons", "icon-192.png"))


async def manifest(request):
    return web.FileResponse(os.path.join(HERE, "manifest.webmanifest"))


async def sw(request):
    return web.FileResponse(os.path.join(HERE, "sw.js"))


async def mic(request):
    # koe-claude 用: iPhone/どの端末でもマイクをリアルタイム送信する送話ページ
    return web.FileResponse(os.path.join(HERE, "mic.html"))


@web.middleware
async def cache_headers(request, handler):
    """/assets/ は CDN/ブラウザに長く持たせる(PRELOADの再取得ゼロ・R2/CDN前置き時に
    エッジがヒットする)。HTML/JS は常に最新(デプロイ直後に古いclientを掴まない)。"""
    resp = await handler(request)
    p = request.path
    if p.startswith("/assets/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    elif p in ("/", "/screen", "/admin", "/dj", "/preview", "/sw.js", "/mic", "/flags"):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


# ---- show control: OSC in / DMX out ------------------------------------------
async def osc_dispatch(address, args, at, ch):
    """OSC アドレス → 内部 API。HTTP と同じ本体(do_*)を呼ぶので挙動は同一。
    at = バンドルの未来 timetag(あれば cue/light の基準時刻に使う)。認証なし=LANの卓専用。"""
    state = _chan(ch)
    a = list(args)

    def f(i, default=None):
        return float(a[i]) if len(a) > i and a[i] is not None else default

    if address == "/soluna/cue":
        if not a:
            raise BadRequest("/soluna/cue <url> [lead] [gain]")
        body = {"url": str(a[0]), "gain": f(2, 1.0)}
        if at:
            body["at"] = at
        elif f(1) is not None:
            body["lead"] = f(1)
        return await do_cue(state, body)
    if address == "/soluna/preload":
        return await do_cue(state, {"url": str(a[0]), "preload": True})
    if address == "/soluna/stop":
        return await do_cue(state, {"stop": True})
    if address == "/soluna/go":
        return await do_show(state, {"next": True})
    if address == "/soluna/show/goto":
        return await do_show(state, {"goto": int(a[0])})
    if address == "/soluna/light":
        body = {"pattern": str(a[0]) if a else "pulse"}
        cols = [str(c) for c in a[1:3] if isinstance(c, str)]
        if cols:
            body["colors"] = cols
        if f(3) is not None:
            body["bpm"] = f(3)
        if at:
            body["at"] = at
        return await do_light(state, body)
    if address == "/soluna/light/stop":
        return await do_light(state, {"stop": True})
    if address == "/soluna/align":
        return await do_align(state, f(0, 0.0))
    if address == "/soluna/zone":
        zones = dict(state["zones"])
        zones[str(a[0]).upper()] = round(f(1, 0.0), 1)
        state["zones"] = zones
        _save_state()
        await _broadcast_text(state, _config_msg(state))
        return {"ok": True, "zones": zones}
    if address == "/soluna/tc":
        return do_timecode(state, str(a[0]), f(1, 30.0))
    raise BadRequest(f"unknown OSC address {address} (see showctl.OSC_ADDRESSES)")


async def _start_showctl(app):
    osc_port = int(os.environ.get("SOLUNA_OSC_PORT") or 0)
    if osc_port:
        app["osc"] = await showctl.OscServer.start(osc_dispatch, osc_port)
        print(f"   OSC in: udp/{osc_port}  ({len(showctl.OSC_ADDRESSES)} addresses, no auth — LAN only)")
    artnet = os.environ.get("SOLUNA_ARTNET") or ""
    sacn = os.environ.get("SOLUNA_SACN") or ""
    if artnet or sacn:
        dmx_ch = os.environ.get("SOLUNA_DMX_CH", "festival")
        dmx = showctl.DmxOut(lambda: _chan(dmx_ch).get("light"), artnet=artnet, sacn=sacn,
                             fixtures=int(os.environ.get("SOLUNA_DMX_FIXTURES") or 8),
                             artnet_port=int(os.environ.get("SOLUNA_ARTNET_PORT") or showctl.ARTNET_PORT),
                             sacn_port=int(os.environ.get("SOLUNA_SACN_PORT") or showctl.SACN_PORT),
                             start_ch=int(os.environ.get("SOLUNA_DMX_START") or 1), now=now)
        app["dmx"] = dmx
        app["dmx_task"] = asyncio.ensure_future(dmx.run())
        print(f"   DMX out: {dmx.describe()}  fixtures={dmx.fixtures} (ch {dmx_ch})")


async def _send_to_host(host, msg: dict) -> int:
    """箱(Pi)の名前で名乗っているノードWSへ1件送る(setup の🔔テスト音など)。→ 送れた本数"""
    text = json.dumps(msg)
    n = 0
    for st in channels.values():
        for ws, meta in list(st["listeners"].items()):
            if meta.get("host") == host:
                try:
                    await asyncio.wait_for(ws.send_str(text), timeout=2.0)
                    n += 1
                except Exception:
                    pass
    return n


def main():
    port = int(os.environ.get("PORT", "8900"))
    os.makedirs(ASSETS, exist_ok=True)
    _load_state()   # クラッシュ/再起動からショー状態を復元
    middlewares = [cache_headers]
    BOX = os.environ.get("SOLUNA_BOX") == "1"                  # Raspberry Pi 箱: /setup と /welcome を出す
    CAPTIVE_PORT = int(os.environ.get("SOLUNA_CAPTIVE_PORT") or 0)   # 箱のAPで :80 も聞く(接続→自動でページが開く)
    if BOX:
        import boxctl
        if CAPTIVE_PORT or os.environ.get("SOLUNA_CAPTIVE") == "1":
            middlewares.append(boxctl.captive_middleware)
    app = web.Application(client_max_size=256 * 1024 * 1024,   # 映像アップロード対応
                          middlewares=middlewares)
    if BOX:
        boxctl.register(app, {"version": VERSION, "role": lambda: "server",
                              "send_to_host": _send_to_host, "admin_ok": _admin_ok})
    app.on_startup.append(_start_showctl)

    async def _resume_auto(app):
        _auto_resume_all()
        _light_resume_all()
    app.on_startup.append(_resume_auto)
    app.add_routes([
        web.get("/health", health),
        web.get("/api/state", api_state),
        web.post("/api/state", api_state),
        web.delete("/api/channel", api_channel_delete),
        web.get("/", index),
        web.get("/screen", index),   # プロジェクター/LEDウォール用(同じclient・screenモード)
        web.get("/admin", admin_page),
        web.get("/about", about_page),      # 製品サイト(何か・画面・つなぎ方・箱・数字)
        web.get("/connect", connect_page),  # 既存システムの繋げ方(卓/Dante/AES67/OSC/TC/Art-Net/Pi)
        web.get("/production", production_page),  # 制作チーム向け本番運用ガイド(日英)
        web.get("/dj", dj_page),
        web.get("/preview", preview_page),  # DJ42セットリストの非公開プレビュー(映像+演出)
        web.get("/dj42", dj42_page),        # キー(dj42)で6曲を再生するスマホ用プレイヤー
        web.get("/booth", booth_page),      # DJブース用コンソール(セットリスト/ライト/ミュート)
        web.post("/api/dj42", api_dj42),    # キーだけで全端末へ音+光を配信する簡易リモコン
        web.get("/favicon.ico", favicon),
        web.get("/manifest.webmanifest", manifest),
        web.get("/sw.js", sw),
        web.get("/mic", mic),
        web.get("/status", status),
        web.get("/audio", audio_ws),
        web.post("/api/cue", api_cue),
        web.post("/api/zones", api_zones),
        web.get("/api/preload", api_preload),
        web.post("/api/net", api_net),
        web.post("/api/mute", api_mute),
        web.get("/api/stats", api_stats),
        web.post("/api/nodes/report", api_nodes_report),
        web.get("/api/nodes", api_nodes),
        web.post("/api/nodes/assign", api_nodes_assign),
        web.post("/api/align", api_align),
        web.post("/api/light", api_light),
        web.get("/api/light/presets", api_light_presets),
        web.post("/api/light/presets", api_light_presets),
        web.post("/api/geo", api_geo),
        web.post("/api/show", api_show),
        web.get("/api/show", api_show),       # セットリスト読み取り(プレビュー用)
        web.get("/api/timecode", api_timecode),
        web.post("/api/timecode", api_timecode),
        web.get("/flags", flags_page),
        web.get("/api/qr", api_qr),          # 観客シェア用QR(SVG)
        web.get("/guide", guide_page),      # 全URL・使い方・QRを1枚にまとめた案内
        web.get("/api/assets", api_assets),
        web.post("/api/upload", api_upload),
        web.static("/assets", ASSETS),
        web.static("/icons", os.path.join(HERE, "icons")),
        web.static("/ui", os.path.join(HERE, "ui")),          # 共有デザインシステム(soluna.css)
        web.static("/site", os.path.join(HERE, "site")),      # /about 用スクリーンショット
    ])
    ip = os.environ.get("LAN_IP", "127.0.0.1")
    _mdns_publish(port)
    print(f"\n🔊 SOLUNA Sound {VERSION}  http://{ip}:{port}/")
    print(f"   client http://{ip}:{port}/?zone=A   admin http://{ip}:{port}/admin")
    print(f"   data: {DATA_DIR}  assets: {ASSETS}"
          + (f"  asset_base: {ASSET_BASE}" if ASSET_BASE else "") + "\n")
    if BOX and CAPTIVE_PORT and CAPTIVE_PORT != port:
        # 2ポート同時: 通常(:8900)+キャプティブ(:80)。同じ app なので状態は1つ。
        async def _serve():
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            try:
                await web.TCPSite(runner, "0.0.0.0", CAPTIVE_PORT).start()
                print(f"   captive portal also on :{CAPTIVE_PORT}")
            except OSError as e:                 # 権限が無い(AmbientCapabilities未設定)→ 通常ポートだけで続行
                print(f"   captive port {CAPTIVE_PORT} unavailable: {e}")
            while True:
                await asyncio.sleep(3600)
        asyncio.run(_serve())
    else:
        web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
