#!/usr/bin/env bash
# HiveSplit setup: install the test runner, install the CLI, then EXIT.
# Runs unprivileged; installs nothing system-wide and never starts a server.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 not found — install Python 3.9+ first (see INSTALL.md)" >&2
    exit 1
fi

# 1. Upgrade the packaging toolchain first: old pip silently fails editable installs.
"$PYTHON" -m pip install --upgrade pip setuptools wheel

# 2. Declared dependencies (the test runner; HiveSplit itself is stdlib-only).
"$PYTHON" -m pip install -r requirements.txt

# 3. Install the console entry point so `hivesplit` is on PATH.
"$PYTHON" -m pip install -e .

# 4. Fail loudly here rather than at first run.
"$PYTHON" -c "import hivesplit; print('hivesplit import OK', hivesplit.__version__)"

echo
echo "Setup complete. Nothing is running; HiveSplit is a local command."
echo "  Start a store:  $PYTHON hivesplit.py init --apiary \"Willow Yard\""
echo "  Full tour:      bash demo.sh"
echo "  Tests:          $PYTHON -m pytest"
