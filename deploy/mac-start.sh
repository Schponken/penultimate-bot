#!/bin/bash
# Starts Penultimate on a Mac. First run sets everything up. After that the bot runs in the
# background, starts again when you log in, restarts itself if it stops, and keeps the Mac
# from idle-sleeping while it runs. Everything it prints goes to data/launcher.log.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1
mkdir -p data
LOG="$DIR/data/launcher.log"
exec >>"$LOG" 2>&1
echo "== $(date '+%Y-%m-%d %H:%M:%S') start in $DIR"
LABEL="com.penultimate.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://127.0.0.1:8787/"
say_box() { /usr/bin/osascript -e "display dialog \"$1\" with title \"Penultimate\" buttons {\"OK\"} default button 1" >/dev/null 2>&1; }

# 1. find a Python 3.9+
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.homebrew/bin:$HOME/miniconda3/bin:/usr/bin:$PATH"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  if "$p" -c 'import sys,venv; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ] && [ ! -x .venv/bin/python ]; then
  echo "no python found"
  say_box "Penultimate needs Python 3. macOS will now offer to install its developer tools. Accept, wait for it to finish, then open Penultimate again."
  xcode-select --install >/dev/null 2>&1
  exit 1
fi

# 2. private environment (first run, or if it was moved or broken)
if ! .venv/bin/python -c 'import yaml, ccxt' 2>/dev/null; then
  echo "setting up environment with $PY"
  rm -rf .venv
  "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -r requirements.txt
  if ! .venv/bin/python -c 'import yaml, ccxt' 2>/dev/null; then
    echo "environment setup failed"
    say_box "Penultimate could not finish setting up. Check the internet connection and open it again. Details are in data/launcher.log."
    exit 1
  fi
fi

# 3. background service: starts at login, restarts itself, keeps the Mac awake while running
mkdir -p "$HOME/Library/LaunchAgents"
XDIR="$(printf '%s' "$DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
cat > "$PLIST" << PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$XDIR/.venv/bin/python</string><string>-m</string><string>penultimate</string>
  </array>
  <key>WorkingDirectory</key><string>$XDIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$XDIR/data/service.log</string>
  <key>StandardErrorPath</key><string>$XDIR/data/service.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1 || launchctl load -w "$PLIST" 2>&1
echo "service loaded"

# 4. wait for the dashboard, fall back to running directly if the service did not come up
up() { /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; }
for i in $(seq 1 30); do up && break; sleep 1; done
if ! up; then
  echo "service did not answer; starting directly"
  nohup /usr/bin/caffeinate -i .venv/bin/python -m penultimate >> data/service.log 2>&1 &
  for i in $(seq 1 20); do up && break; sleep 1; done
fi
if up; then echo "dashboard up"; open "$URL"; else echo "dashboard not reachable"; say_box "Penultimate did not start. Details are in data/service.log inside its folder."; fi
