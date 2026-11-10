#!/bin/bash
# One-time install of the forecast model's runtime (PyTorch) into its own environment, .venv-forecast.
# The bot runs this by itself in the background. It tries each Python on the machine until one works,
# because PyTorch does not support every Python version.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1
echo "== $(date '+%Y-%m-%d %H:%M:%S') forecast runtime setup"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.homebrew/bin:$HOME/miniconda3/bin:/usr/bin:$PATH"
CANDIDATES="python3.13 python3.12 python3.11 python3.10 python3.14 $HOME/miniconda3/bin/python3 python3"
for c in $CANDIDATES; do
  PY="$(command -v "$c" 2>/dev/null)" || continue
  "$PY" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] else 1)' 2>/dev/null || continue
  echo "trying $PY ($("$PY" -V 2>&1))"
  rm -rf .venv-forecast
  "$PY" -m venv .venv-forecast || continue
  .venv-forecast/bin/python -m pip install --quiet --upgrade pip
  if [ "$(uname -s)" = "Linux" ]; then   # CPU-only build; the default Linux one pulls gigabytes of GPU libraries
    .venv-forecast/bin/python -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu || continue
  fi
  if .venv-forecast/bin/python -m pip install --quiet torch numpy pandas einops huggingface_hub safetensors tqdm \
     && .venv-forecast/bin/python -c 'import torch, pandas, einops, huggingface_hub, safetensors, tqdm; print("torch", torch.__version__)'; then
    touch .venv-forecast/.ready
    echo "forecast runtime ready with $PY"
    exit 0
  fi
  echo "$PY did not work, trying the next one"
done
rm -rf .venv-forecast
echo "No Python on this machine could install PyTorch. Install Python 3.12 (python.org or Homebrew), then restart Penultimate."
exit 1
