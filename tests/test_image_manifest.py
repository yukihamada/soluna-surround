"""Dockerfile ガード: server.py が import するローカルモジュールが全部イメージに COPY されているか。
(v7 で showctl.py の COPY 漏れ → 本番が起動ループで停止。二度と踏まない)"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    ok += cond; fail += (not cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
src = open(os.path.join(ROOT, "server.py")).read()
docker = open(os.path.join(ROOT, "Dockerfile")).read()
copied = set(re.findall(r"[\w./-]+", " ".join(l[5:] for l in docker.splitlines() if l.startswith("COPY"))))
local_py = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
imported = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][\w]*)", src, re.M))
for mod in sorted(imported & local_py):
    check(f"Dockerfile COPY {mod}.py", f"{mod}.py" in copied)
for page in ("client.html", "admin.html", "dj.html", "mic.html", "sw.js", "manifest.webmanifest"):
    check(f"Dockerfile COPY {page}", page in copied)
check("Dockerfile pip has aiohttp+qrcode", "aiohttp" in docker and "qrcode" in docker)
check("Dockerfile COPY ui (shared css)", "ui" in copied)
print(f"\n== PASS {ok} / FAIL {fail} ==")
sys.exit(1 if fail else 0)
