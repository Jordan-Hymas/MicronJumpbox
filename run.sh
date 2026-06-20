#!/usr/bin/env bash
# Quick launcher for testing: runs jumpbox from this project's venv.
# Deploys to Linux, but the venv here may be a Windows one (Scripts/) while
# developing locally - check both layouts.
# Usage:  bash run.sh
dir="$(dirname "$0")"
if [ -x "$dir/.venv/bin/jumpbox" ]; then
    exec "$dir/.venv/bin/jumpbox"
else
    exec "$dir/.venv/Scripts/jumpbox.exe"
fi
