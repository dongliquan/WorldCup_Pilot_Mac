#!/usr/bin/env bash
# Dev run: native WebKit window via pywebview.
set -euo pipefail
cd "$(dirname "$0")"
# prefer the project venv (pywebview + pyobjc live here); fall back to system python
if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python worldcup.py
else
  exec python3 worldcup.py
fi
