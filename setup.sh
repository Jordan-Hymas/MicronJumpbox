#!/usr/bin/env bash
# One-time setup: creates a venv and installs Jumpbox into it.
#
# ── What the machine (e.g. the jump-host VM) needs ─────────────────────
#
#  1. Linux with Python 3.10 or newer, including the venv module.
#       Debian/Ubuntu:  sudo apt install python3 python3-venv
#       RHEL/Rocky 9:   sudo dnf install python3.11
#     3.10 is a hard floor (the code uses runtime `X | Y` type unions;
#     RHEL 9's default python3 is 3.9 and would crash at import). If the
#     default python3 is too old, point this script at a newer one:
#       PYTHON=python3.11 bash setup.sh
#
#  2. tmux - every host connection opens in its own tmux pane, and the
#     fullscreen button uses tmux's zoom (any tmux from the last decade,
#     >= 1.8, has it).
#       Debian/Ubuntu:  sudo apt install tmux
#       RHEL/Rocky:     sudo dnf install tmux
#
#  3. The OpenSSH client (`ssh`) - present on any Linux server. Jumpbox
#     runs plain `ssh` in each pane; it never stores credentials. For
#     passwordless access this VM's sshd should allow agent forwarding
#     (AllowAgentForwarding yes - the usual default) and users connect
#     with "Forward SSH agent" enabled in MobaXterm. See the README's
#     "Passwordless access" section.
#
#  4. Either network access to PyPI (direct or via the corporate
#     proxy/mirror), OR a prefilled ./wheels folder for a fully offline
#     install. To prefill it, on any machine WITH internet (same Python
#     major.minor as this one):
#         python3 -m pip download -r requirements.txt -d wheels
#     then copy the whole project folder (including wheels/) over -
#     this script detects wheels/ automatically and never touches the
#     network.
#
#  Nothing else. The app has no compiled/native dependencies, and its
#  data lives per-user in ~/.jumpbox (created on first run; the real
#  inventory then loads with `jumpbox import hosts.csv --replace`).
#
#  Network reachability to the actual switches/APs/routers is a separate,
#  environment-level concern - see docs/DEPLOYMENT_PLAN.md.
#
# Re-run this script after updating the code (git pull / new tarball):
# it installs a snapshot of the package, not a live link to the source.
# (Developers who want edits picked up immediately: .venv/bin/pip
# install -e . --no-build-isolation)
#
# Usage:  bash setup.sh              (or PYTHON=python3.11 bash setup.sh)
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "$PYTHON not found - install it first (e.g. sudo apt install python3 python3-venv)" >&2
    exit 1
fi

# Hard floor: 3.10+ (runtime `X | Y` unions in the dialog code). Checked
# here so a too-old interpreter fails with one clear line instead of a
# traceback on first launch.
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Jumpbox needs Python 3.10+, but $PYTHON is $("$PYTHON" -V 2>&1)." >&2
    echo "Install a newer one (e.g. sudo dnf install python3.11) and re-run as:" >&2
    echo "  PYTHON=python3.11 bash setup.sh" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found - Jumpbox needs it to open host connections in their own panes (e.g. sudo apt install tmux)" >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating virtual environment in .venv ..."
    "$PYTHON" -m venv .venv
fi

# Deploys to Linux (bin/), but the venv may be a Windows one (Scripts/)
# while developing locally in Git Bash - check both layouts.
if [ -x .venv/bin/pip ]; then
    pip=.venv/bin/pip
else
    pip=.venv/Scripts/pip.exe
fi

# Offline mode: a ./wheels folder (see header) means every install below
# runs with --no-index and never touches the network.
offline_args=()
if [ -d wheels ]; then
    echo "Found ./wheels - installing offline from it (no network access needed)."
    offline_args=(--no-index --find-links wheels)
else
    # Best-effort only: an old pip still works fine for these installs,
    # so a blocked PyPI shouldn't kill setup at this step.
    "$pip" install --quiet --upgrade pip || true
fi

echo "Installing dependencies (textual, rich, build tools)..."
"$pip" install --quiet "${offline_args[@]}" -r requirements.txt

# --no-build-isolation: build with the setuptools/wheel just installed
# from requirements.txt, instead of pip downloading a build backend from
# PyPI mid-install - which would break exactly when offline.
echo "Installing Jumpbox..."
"$pip" install --quiet "${offline_args[@]}" --no-build-isolation .

echo
echo "Setup complete. To run Jumpbox:"
echo "  bash run.sh"
echo
echo "To verify everything works first (fully offline-safe):"
echo "  .venv/bin/python -m tests.smoke"
echo
echo "To load the real inventory from a CSV:"
echo "  .venv/bin/jumpbox template hosts.csv     # see the expected columns"
echo "  .venv/bin/jumpbox import hosts.csv --replace"
