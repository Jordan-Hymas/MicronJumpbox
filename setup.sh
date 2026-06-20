#!/usr/bin/env bash
# One-time setup: creates a venv and installs Jumpbox into it. Deploys to
# Linux, but also used locally on Windows (Git Bash) while developing.
# Usage:  bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found - install it first (e.g. sudo apt install python3 python3-venv)" >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating virtual environment in .venv ..."
    python3 -m venv .venv
fi

if [ -x .venv/bin/pip ]; then
    pip=.venv/bin/pip
else
    pip=.venv/Scripts/pip.exe
fi

echo "Installing dependencies..."
"$pip" install --quiet --upgrade pip
"$pip" install --quiet -r requirements.txt
"$pip" install --quiet .

echo
echo "Setup complete. To run Jumpbox:"
echo "  bash run.sh"
echo
echo "To verify everything works first:"
echo "  .venv/bin/python -m tests.smoke"
