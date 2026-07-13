#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -n "${PYTHON:-}" ]; then
  :
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
  PYTHON=python3.11
elif command -v python3.10 >/dev/null 2>&1; then
  PYTHON=python3.10
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Open Health Agent needs Python 3.10 or newer." >&2
  exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/scripts/install.py" "$@"
