#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
printf 'Open http://localhost:8000 in your browser.\n'
exec .venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
