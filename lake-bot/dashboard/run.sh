#!/bin/bash
# run.sh — Start the Lake Bot dashboard server
# Usage: ./run.sh <pi-ip-address>
# Example: ./run.sh 192.168.1.42

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PI_HOST="${1}"
if [ -z "$PI_HOST" ]; then
  echo "Error: Pi host address required."
  echo "Usage: ./run.sh <pi-ip-address>"
  echo "Example: ./run.sh 192.168.1.42"
  exit 1
fi

VENV_DIR="$SCRIPT_DIR/.venv"

# Create venv if not present
if [ ! -d "$VENV_DIR" ]; then
  echo "[run.sh] Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Install/upgrade dependencies
echo "[run.sh] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "[run.sh] Starting dashboard server targeting Pi at: $PI_HOST"
echo "[run.sh] Dashboard will be at: http://localhost:8080"
echo ""

python3 server.py --pi-host "$PI_HOST"
