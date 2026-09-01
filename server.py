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
import struct
import time
import os
from aiohttp import web, WSMsgType

SR = 48000
# live mode: schedule this far ahead of "now"。既存ハウスPAとの融合時は
# 「パイプライン遅延 ≦ グリッド最前ゾーンの物理遅延(d/343)」が条件なので
# 有線LANなら SOLUNA_LEAD=0.08 程度まで詰める。push hello {"lead":..} でも上書き可。
LEAD = float(os.environ.get("SOLUNA_LEAD", "0.6"))
CUE_LEAD = 3.0                  # cue mode: default lead so every device can arm
HEADER = struct.Struct("<3sBBBIId")
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")

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
        "base_ms": 0.0,           # 全ゾーン一括トリム(既存ハウスPAとの位相合わせ)
        "lead": LEAD,
        "cue": None,              # active cue dict (途中参加者へ再送する)
        "light": None,            # active light dict (SOLUNAモード: 色の同期)
        "geo": None,              # {"lat","lng"} ステージ位置(GPSゾーン自動選択用)
        "preload": None,          # 事前配布済み音源URL(FIRE前のDLバースト回避)
    })


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


def _config_msg(state):
    return json.dumps({"t": "config", "zones": state["zones"], "sr": state["sr"],
                       "base_ms": state["base_ms"], "geo": state["geo"],
                       "server_ms": time.time() * 1000.0})


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
            "d": request.query.get("d")}
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
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get("t") == "ping":
                    await ws.send_str(json.dumps({
                        "t": "pong", "c": m.get("c"), "s": time.time() * 1000.0
                    }))
                elif m.get("t") == "pos":
                    meta["pos"] = m.get("pos", meta["pos"])
                elif m.get("t") == "zone":
                    meta["zone"] = (m.get("zone") or "").upper() or None
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

    now = time.time()
    if state["epoch"] is None:
        state["epoch"] = now + state["lead"]
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

async def api_cue(request):
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch", "festival")
    state = _chan(name)
    body = await request.json()

    if body.get("stop"):
        state["cue"] = None
        await _broadcast_text(state, json.dumps({"t": "cue_stop"}))
        return web.json_response({"ok": True, "stopped": True,
                                  "listeners": len(state["listeners"])})

    url = body.get("url")            # 音声(WebAudio=サンプル精度)
    video = body.get("video")        # 映像(video要素=クロックにドリフト補正で追従)
    if not url and not video:
        raise web.HTTPBadRequest(text="url (audio) and/or video required")

    if body.get("preload"):
        url = url or video
        # 事前配布: 全端末がDL/デコードだけ済ませる(再生しない)。本番のFIREは
        # 一斉DLバーストなしで頭から揃う。開演30分前に打っておくのが正。
        state["preload"] = url
        await _broadcast_text(state, json.dumps({"t": "preload", "url": url}))
        return web.json_response({"ok": True, "preloaded": url,
                                  "listeners": len(state["listeners"])})
    if body.get("at"):                       # サーバepoch秒を直接指定
        at = float(body["at"])
    else:                                    # lead秒後(サーバ時計基準・推奨)
        at = time.time() + float(body.get("lead") or CUE_LEAD)
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
    state["cue"] = cue
    await _broadcast_text(state, json.dumps({"t": "cue", **cue}))
    return web.json_response({"ok": True, "cue": cue,
                              "listeners": len(state["listeners"])})


async def api_light(request):
    """SOLUNAモード(色の同期)。色データは配信せず「パターン+開始時刻」だけを
    配り、各端末が (ゾーン位置, 同期時刻) から色をローカル計算する — 音のCUEと
    同じ思想で帯域ゼロ・台数無制限。
    body: {pattern: solid|pulse|wave|plasma|strobe|audio,
           colors: ["#rrggbb", ...], bpm, speed, brightness, at?} or {stop:true}
    pattern=audio は再生中キューの音源から端末側がエネルギー包絡を解析して
    明るさに変換する(追加通信なし)。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch", "festival")
    state = _chan(name)
    body = await request.json()

    if body.get("stop"):
        state["light"] = None
        await _broadcast_text(state, json.dumps({"t": "light_stop"}))
        return web.json_response({"ok": True, "stopped": True,
                                  "listeners": len(state["listeners"])})

    pattern = body.get("pattern", "pulse")
    if pattern not in ("solid", "pulse", "wave", "plasma", "strobe", "audio"):
        raise web.HTTPBadRequest(text="pattern must be one of "
                                      "solid|pulse|wave|plasma|strobe|audio")
    light = {
        "id": body.get("id") or f"light-{int(time.time() * 1000)}",
        "pattern": pattern,
        "colors": body.get("colors") or ["#d4af37", "#7fc9a2"],
        "bpm": float(body.get("bpm", 120)),
        "speed": float(body.get("speed", 1.0)),
        "brightness": float(body.get("brightness", 1.0)),
        "at": float(body.get("at") or time.time()),   # パターン位相の基準時刻
    }
    state["light"] = light
    await _broadcast_text(state, json.dumps({"t": "light", **light}))
    return web.json_response({"ok": True, "light": light,
                              "listeners": len(state["listeners"])})


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
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "geo": state["geo"],
                              "listeners": len(state["listeners"])})


async def api_align(request):
    """既存ハウスPAとの位相合わせ: 全ゾーン一括トリム(ms, 負値=早める方向)。
    サウンドチェックでクリックをハウスPA+グリッド同時に鳴らし、フラム感が
    消えるまで ±5ms 刻みで追い込む。"""
    if not _admin_ok(request):
        raise web.HTTPForbidden(text="x-soluna-admin required (set SOLUNA_ADMIN)")
    name = request.query.get("ch", "festival")
    state = _chan(name)
    body = await request.json()
    state["base_ms"] = float(body.get("base_ms", 0.0))
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "base_ms": state["base_ms"],
                              "listeners": len(state["listeners"])})


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
    if not isinstance(zones, dict) or not zones:
        raise web.HTTPBadRequest(
            text='{"zones":{"A":15.0,...}} (delay_ms) or {"zones_m":{"A":15,...}} (距離m)')
    state["zones"] = {str(k).upper(): round(float(v), 1) for k, v in zones.items()}
    await _broadcast_text(state, _config_msg(state))
    return web.json_response({"ok": True, "zones": state["zones"]})


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


async def status(request):
    out = {}
    for name, st in channels.items():
        zones_n = {}
        for meta in st["listeners"].values():
            key = meta.get("zone") or meta.get("pos") or "?"
            zones_n[key] = zones_n.get(key, 0) + 1
        out[name] = {
            "source": st["source"] is not None,
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
            "geo": st["geo"],
        }
    return web.json_response({"server_epoch_ms": time.time() * 1000.0,
                              "lead": LEAD, "cue_lead": CUE_LEAD, "channels": out})


async def index(request):
    return web.FileResponse(os.path.join(HERE, "client.html"))


async def admin_page(request):
    return web.FileResponse(os.path.join(HERE, "admin.html"))


async def dj_page(request):
    return web.FileResponse(os.path.join(HERE, "dj.html"))


async def favicon(request):
    return web.FileResponse(os.path.join(HERE, "icons", "icon-192.png"))


async def manifest(request):
    return web.FileResponse(os.path.join(HERE, "manifest.webmanifest"))


async def sw(request):
    return web.FileResponse(os.path.join(HERE, "sw.js"))


async def mic(request):
    # koe-claude 用: iPhone/どの端末でもマイクをリアルタイム送信する送話ページ
    return web.FileResponse(os.path.join(HERE, "mic.html"))


def main():
    port = int(os.environ.get("PORT", "8900"))
    os.makedirs(ASSETS, exist_ok=True)
    app = web.Application(client_max_size=256 * 1024 * 1024)   # 映像アップロード対応
    app.add_routes([
        web.get("/", index),
        web.get("/screen", index),   # プロジェクター/LEDウォール用(同じclient・screenモード)
        web.get("/admin", admin_page),
        web.get("/dj", dj_page),
        web.get("/favicon.ico", favicon),
        web.get("/manifest.webmanifest", manifest),
        web.get("/sw.js", sw),
        web.get("/mic", mic),
        web.get("/status", status),
        web.get("/audio", audio_ws),
        web.post("/api/cue", api_cue),
        web.post("/api/zones", api_zones),
        web.post("/api/align", api_align),
        web.post("/api/light", api_light),
        web.post("/api/geo", api_geo),
        web.get("/api/assets", api_assets),
        web.post("/api/upload", api_upload),
        web.static("/assets", ASSETS),
        web.static("/icons", os.path.join(HERE, "icons")),
    ])
    ip = os.environ.get("LAN_IP", "127.0.0.1")
    print(f"\n🔊 SOLUNA Surround v3  http://{ip}:{port}/")
    print(f"   client http://{ip}:{port}/?zone=A   admin http://{ip}:{port}/admin")
    print(f"   assets: {ASSETS}\n")
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
