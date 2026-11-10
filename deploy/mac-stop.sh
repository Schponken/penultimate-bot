#!/bin/bash
# Stops Penultimate and removes it from login start-up. Open positions and resting orders are NOT
# touched: press "Stop everything" on the dashboard first if you want to be flat.
LABEL="com.penultimate.bot"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "python -m penultimate" 2>/dev/null
/usr/bin/osascript -e 'display dialog "Penultimate is stopped and will not start at login." with title "Penultimate" buttons {"OK"} default button 1' >/dev/null 2>&1
