#!/bin/bash
# Double-click to start Penultimate (same as opening the Penultimate app).
/bin/bash "$(cd "$(dirname "$0")" && pwd)/deploy/mac-start.sh"
echo "Penultimate is running in the background. You can close this window."
