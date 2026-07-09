#!/bin/bash
# SOLUNA Surround — double-click to launch everything and auto-play.
# Starts the server + a demo source, then opens 3 native speakers (L / C / R).
# On one Mac you hear the blip move in stereo; for real 3-device surround run
# play.py on each Mac, or open http://<this-ip>:8900/?pos=L|C|R on phones.
cd "$(dirname "$0")" || exit 1
PY=python3
PORT=${PORT:-8900}
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)

echo "🔊 SOLUNA Surround  —  http://$IP:$PORT/"

# 1) server (skip if already up)
if ! curl -s -m2 "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
  LAN_IP="$IP" PORT="$PORT" nohup $PY server.py > server.log 2>&1 &
  sleep 1.5
fi

# 2) demo source (restart to be safe)
pkill -f "soluna-surround/source.py" 2>/dev/null
nohup $PY source.py --test --ch festival --server "ws://127.0.0.1:$PORT" > source.log 2>&1 &
sleep 1

# 3) three native speakers, panned to their positions
pkill -f "soluna-surround/play.py" 2>/dev/null
for POS in L C R; do
  nohup $PY -u play.py "$POS" --server "ws://127.0.0.1:$PORT" --ch festival > "play_$POS.log" 2>&1 &
done
sleep 1

echo "▶ playing L / C / R  (blip rotates left→center→right)"
echo "   phones on same Wi-Fi:  http://$IP:$PORT/?pos=C   (tap ▶)"
echo "   stop:  ./stop.command"
