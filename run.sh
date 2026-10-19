#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/tianfu-pycache"
exec python3 run.py
