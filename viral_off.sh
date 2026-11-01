#!/bin/bash
# Viral Finder — OFF.  Stops the local server and reports what it freed.

PORT="8765"
URL="http://127.0.0.1:$PORT"

before=$(ps -Ao rss= | awk '{s+=$1} END {print int(s/1024)}')

if ! curl -s -m 2 -o /dev/null "$URL/"; then
  osascript -e 'display notification "Was not running." with title "Viral Finder"'
  exit 0
fi

# Ask the server to stop itself (it exits ~0.4s after answering).
curl -s -m 3 -o /dev/null "$URL/quit"
for _ in $(seq 1 24); do
  curl -s -m 1 -o /dev/null "$URL/" || break
  sleep 0.25
done

# Fallback: kill whatever still holds the port.
PIDS=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null && sleep 1

if curl -s -m 2 -o /dev/null "$URL/"; then
  osascript -e 'display alert "Viral Finder" message "Could not stop the server. Quit it from /tmp/viral_finder.log window or restart the Mac."'
else
  osascript -e 'display notification "Server stopped." with title "Viral Finder"'
fi

# Close any leftover job runners from the app.
pkill -f "clipper-tools/find_viral.py" 2>/dev/null
rm -f /tmp/viral_finder.log 2>/dev/null
exit 0
