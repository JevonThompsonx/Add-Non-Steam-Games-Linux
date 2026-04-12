#!/usr/bin/env bash
# run.sh — Launch the Steam Non-Steam Game Manager (Linux / CachyOS)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Python interpreter ──────────────────────────────────────────────────────
# Prefer python3 from the active virtual environment, then system python3.
if [[ -x "$SCRIPT_DIR/.venv/bin/python3" ]]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "ERROR: python3 not found. Install Python 3.10+ and try again." >&2
    exit 1
fi

# ── Virtual environment bootstrap (first run) ───────────────────────────────
if [[ ! -x "$SCRIPT_DIR/.venv/bin/python3" ]]; then
    echo "Setting up virtual environment..."
    "$PYTHON" -m venv "$SCRIPT_DIR/.venv"
    "$SCRIPT_DIR/.venv/bin/pip" install --quiet --upgrade pip
    "$SCRIPT_DIR/.venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
fi

# ── .env file ───────────────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" && -f "$SCRIPT_DIR/.env.example" ]]; then
    echo "No .env file found. Copy .env.example to .env and add your STEAMGRIDDB_API_KEY."
    echo "  cp .env.example .env && \$EDITOR .env"
fi

exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
