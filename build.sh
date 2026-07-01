#!/usr/bin/env bash
# Build the macOS .app bundle for World Cup Pilot.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> installing deps"
pip3 install -r requirements.txt

echo "==> building .app (PyInstaller)"
pyinstaller --noconfirm worldcup.spec

echo "==> done: dist/World Cup Pilot.app"
echo "    open \"dist/World Cup Pilot.app\""
