#!/bin/bash
# Viral Finder — ON.  Starts the local server in the background (no window)
# and opens the page in the default browser. Safe to double-click twice.

DIR="/Volumes/SanDiskSSD/clipper-tools"
PORT="8765"
URL="http://127.0.0.1:$PORT"
LOG="/tmp/viral_finder.log"

cd "$DIR" || { osascript -e 'display alert "Viral Finder" message "The SanDiskSSD drive is not connected."'; exit 1; }

# Already running? Just bring the page up.
if curl -s -m 2 -o /dev/null "$URL/"; then
  open "$URL"
  exit 0
fi

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -z "$PY" ] && { osascript -e 'display alert "Viral Finder" message "Could not find python3 on this Mac."'; exit 1; }

# Detached so it keeps running after this script returns.
nohup "$PY" viral_server.py >>"$LOG" 2>&1 &

# Wait until it actually answers before opening the page (python can take ~15s).
for _ in $(seq 1 90); do
  curl -s -m 1 -o /dev/null "$URL/" && break
  sleep 0.5
done

if curl -s -m 2 -o /dev/null "$URL/"; then
  open "$URL"
else
  osascript -e 'display alert "Viral Finder" message "The server did not start. See /tmp/viral_finder.log"'
fi
