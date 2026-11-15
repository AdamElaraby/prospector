#!/bin/bash
# Builds Prospector.app — a double-clickable launcher for the local web UI.
#
# The app is compiled on this machine rather than downloaded, so macOS does not
# quarantine it and Gatekeeper does not need to be argued with.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/Prospector.app"
PORT="${PORT:-8787}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Prospector</string>
  <key>CFBundleDisplayName</key><string>Prospector</string>
  <key>CFBundleIdentifier</key><string>local.prospector.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Prospector</string>
  <key>CFBundleIconFile</key><string>appicon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <!-- No Dock tile or menu bar: the UI is the browser window. -->
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Prospector" <<LAUNCHER
#!/bin/bash
# Launcher stub. Points at the project it was built from.
PROJECT="$ROOT"
PORT="$PORT"
LAUNCHER
cat >> "$APP/Contents/MacOS/Prospector" <<'LAUNCHER'
fail() {
  osascript -e "display dialog \"$1\" with title \"Prospector\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1
  exit 1
}

# Every guard runs before anything touches the filesystem, so a moved project
# folder produces an explanation rather than a silent no-op on double-click.
[ -d "$PROJECT" ] || fail "Can't find the project folder:\n$PROJECT\n\nIf you moved or renamed it, rebuild this app: open Terminal there and run  make app"
cd "$PROJECT" 2>/dev/null || fail "Can't open the project folder:\n$PROJECT"
[ -x "$PROJECT/.venv/bin/python" ] || fail "Setup hasn't been run yet.\n\nOpen Terminal in the project folder and run:\n    make setup"

LOG="$PROJECT/out/ui.log"
STATE="$PROJECT/out/ui.url"
mkdir -p "$PROJECT/out" 2>/dev/null || fail "Can't write to:\n$PROJECT/out"

# Is a Prospector we can actually use already up? A live port is not enough —
# the saved link carries a token, and a restarted server invalidates the old
# one. Only a 200 on the saved link proves it is ours and still valid.
existing_url() {
  [ -f "$STATE" ] || return 1
  local url code
  url="$(cat "$STATE" 2>/dev/null)"
  [ -n "$url" ] || return 1
  code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$url" 2>/dev/null)"
  [ "$code" = "200" ] || return 1
  echo "$url"
}

# Second double-click: just bring the tab back and get out of the way, rather
# than starting another server and stacking a second dialog on the first.
if URL="$(existing_url)"; then
  open "$URL"
  exit 0
fi

# The saved link is stale or absent. If our default port is taken by something
# else, move over rather than dying on it.
free_port() {
  "$PROJECT/.venv/bin/python" - "$PORT" <<'PYPORT'
import socket, sys
start = int(sys.argv[1])
for candidate in range(start, start + 10):
    sock = socket.socket()
    # Match the server's own SO_REUSEADDR, otherwise a socket still in
    # TIME_WAIT reads as busy here and the port drifts on every restart.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", candidate))
        print(candidate)
        break
    except OSError:
        continue
    finally:
        sock.close()
PYPORT
}
PORT="$(free_port)"
[ -n "$PORT" ] || fail "Ports are all busy. Close whatever is using them and try again."

"$PROJECT/.venv/bin/python" -m prospector.web --port "$PORT" --no-open > "$LOG" 2>&1 &
SERVER_PID=$!

URL=""
for _ in $(seq 1 60); do
  URL=$(grep -o "http://127.0.0.1:$PORT/?token=[A-Za-z0-9_-]*" "$LOG" 2>/dev/null | head -1)
  [ -n "$URL" ] && break
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 0.25
done

if [ -z "$URL" ]; then
  DETAIL=$(tail -4 "$LOG" 2>/dev/null | tr '"' "'" | tr '\n' ' ')
  fail "Prospector couldn't start.\n\n$DETAIL"
fi

echo "$URL" > "$STATE"
open "$URL"

# This app has no window of its own, so one dialog holds it open and gives you
# a way to shut the server down. Closing it stops Prospector.
while true; do
  CHOICE=$(osascript -e "display dialog \"Prospector is running.

$URL

Leave this open while you work. Click Stop when you are done.\" with title \"Prospector\" buttons {\"Stop\", \"Open in browser\"} default button \"Open in browser\" with icon note" 2>/dev/null) || break
  case "$CHOICE" in
    *"Open in browser"*) open "$URL" ;;
    *) break ;;
  esac
done

kill "$SERVER_PID" 2>/dev/null
rm -f "$STATE"
LAUNCHER

chmod +x "$APP/Contents/MacOS/Prospector"

# Icon: a magnifying glass over a map pin, rendered at the sizes macOS wants.
ICONSET="$(mktemp -d)/appicon.iconset"
mkdir -p "$ICONSET"
cat > "$ICONSET/icon.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" rx="228" fill="#1d2320"/>
  <path d="M436 196c-104 0-188 84-188 188 0 141 188 336 188 336s188-195 188-336c0-104-84-188-188-188z" fill="#a8481a"/>
  <circle cx="436" cy="378" r="74" fill="#1d2320"/>
  <circle cx="648" cy="648" r="150" fill="none" stroke="#f2f4f1" stroke-width="52"/>
  <line x1="754" y1="754" x2="880" y2="880" stroke="#f2f4f1" stroke-width="62" stroke-linecap="round"/>
</svg>
SVG
if command -v qlmanage >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  for size in 16 32 64 128 256 512; do
    qlmanage -t -s $((size*2)) -o "$ICONSET" "$ICONSET/icon.svg" >/dev/null 2>&1 || true
    SRC="$ICONSET/icon.svg.png"
    if [ -f "$SRC" ]; then
      sips -z $size $size "$SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
      sips -z $((size*2)) $((size*2)) "$SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
    fi
  done
  rm -f "$ICONSET/icon.svg" "$ICONSET/icon.svg.png"
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/appicon.icns" 2>/dev/null || true
fi

touch "$APP"
echo "built $APP"
echo "Double-click it, or drag it to your Dock / Applications."
