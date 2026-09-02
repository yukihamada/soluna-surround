#!/usr/bin/env python3
"""play.py(スピーカーノード)のオフライン単体検証: デコード・CUE配置・途中参加・ゾーン絞り・音量補正。
音デバイス不要。  python3 tests/test_node.py"""
import io, os, sys, wave, struct
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import play
from play import Player, decode_to_f32_mono, SR

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name); print(f"  {'✅' if cond else '❌'} {name} {detail}")

def make_wav(sr=44100, ch=2, secs=1.0):
    n = int(sr*secs); t = np.arange(n)/sr
    y = (np.sin(2*np.pi*440*t)*0.5*32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(np.repeat(y, ch).tobytes())
    return buf.getvalue()

# 1) decode: 44.1k stereo wav → 48k mono
y = decode_to_f32_mono(make_wav(), SR)
check("decode wav 44.1k→48k mono 長さ±1%", abs(len(y) - SR) < SR*0.01, f"n={len(y)}")
check("decode 振幅 0.5〜0.71(ffmpegダウンミックス0.707含む)", 0.45 < float(np.abs(y).max()) < 0.75, f"peak={np.abs(y).max():.3f}")

# 2) 既存API互換
p = Player("L", zone="B"); p.zones = {"B": 100.0}; p.base_ms = -20.0
check("delay_sec = (100-20)/1000", abs(p.delay_sec() - 0.08) < 1e-9)
p.base_ms = -200.0; check("delay 負値は0クリップ", p.delay_sec() == 0.0)

# 3) CUE配置: 疑似クロック(t0_stream=0, t0_wall=1000, epoch_off=5 → server_epoch = wall+5)
p = Player("L", zone="B"); p.zones = {"B": 58.7}; p.base_ms = 0.0
p.t0_stream = 0.0; p.t0_wall = 1000.0; p.epoch_off = 5.0
p.cache["/assets/x.wav"] = np.ones(SR*2, dtype=np.float32) * 0.25    # 2秒・定数
at = 1000.0 + 5.0 + 3.0                                               # server epoch: 「wall 1003」
p.arm_cue({"id": "c1", "url": "/assets/x.wav", "at": at, "gain": 1.0})
exp_start = int(round((3.0 + 0.0587) * SR))                            # wall 1003 + zone delay → stream sample
check("cue start = (at+delay-epoch_off - t0_wall)*SR", p.cue["start"] == exp_start, f"{p.cue['start']} vs {exp_start}")
check("state=playing", p.state == "playing")
# 4) ブロック生成: 開始前は無音 / 開始後は 0.25*pan
blk = p.cue_block(p.cue, exp_start - 480, 480)
check("開始前ブロック=無音", float(np.abs(blk).max()) == 0.0)
blk = p.cue_block(p.cue, exp_start, 480)
check("開始後ブロック L=0.25, R=0(pan L)", abs(blk[0,0]-0.25) < 1e-6 and blk[0,1] == 0.0, f"{blk[0]}")
# 5) 途中参加: start が過去でも pos は曲中(=at からの経過分)
blk = p.cue_block(p.cue, exp_start + SR, 480)      # 1秒後
check("途中参加位置=曲中(1s)は音あり", abs(blk[0,0]-0.25) < 1e-6)
blk = p.cue_block(p.cue, exp_start + 3*SR, 480)    # 3秒後=曲(2s)終了後
check("曲終了後=無音 & state→idle", float(np.abs(blk).max()) == 0.0 and p.state == "idle")
# 6) loop
p.arm_cue({"id": "c2", "url": "/assets/x.wav", "at": at, "gain": 1.0, "loop": True})
blk = p.cue_block(p.cue, exp_start + 5*SR, 480)
check("loop: 5秒後も音あり", abs(blk[0,0]-0.25) < 1e-6)
# 7) ゾーン絞り(ウォークテスト)
p.stop_cue()
r = p.arm_cue({"id": "w", "url": "/assets/x.wav", "at": at, "zones": ["A"]})
check("他ゾーン宛cueは無視", r is False and p.cue is None)
r = p.arm_cue({"id": "w", "url": "/assets/x.wav", "at": at, "zones": ["b"]})
check("自ゾーン宛(小文字でも)は再生", r is True and p.cue is not None)
# 8) 音量補正: ノード-6dB + ゾーン+6dB = 0dB
p.gain_db = -6.0; p.zone_gain_db = {"B": 6.0}
check("level_gain(-6+6dB)=1.0", abs(p.level_gain() - 1.0) < 1e-6)
p.gain_db = -20.0; p.zone_gain_db = {}
check("level_gain(-20dB)=0.1", abs(p.level_gain() - 0.1) < 1e-6)
# 9) 映像のみcue / 同期前cue
check("映像のみcueは無視", p.arm_cue({"id": "v", "video": "/assets/x.mp4", "at": at}) is False)
q = Player("C", zone="A")
check("同期前cueは保留(cue_msg保持)", q.arm_cue({"id": "z", "url": "/assets/x.wav", "at": at}) is False and q.cue_msg is not None)
# 10) report 形
rep = p.report()
check("report kind=node st有り", rep["t"] == "report" and rep["kind"] == "node" and rep["st"] in ("idle","playing","preloaded","failed"))
# 11) resolve_url: asset_base 優先
p.http_base = "http://10.0.0.1:8900"; p.asset_base = None
check("resolve_url サーバ直", p.resolve_url("/assets/a.mp3") == "http://10.0.0.1:8900/assets/a.mp3")
p.asset_base = "https://cdn.example.com/soluna"
check("resolve_url asset_base", p.resolve_url("/assets/a.mp3") == "https://cdn.example.com/soluna/a.mp3")

print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
if ng: print("FAILED:", ng); sys.exit(1)
