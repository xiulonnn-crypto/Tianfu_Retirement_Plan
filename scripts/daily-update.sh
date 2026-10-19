#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/tianfu-pycache"
python3 manage.py daily >> data/sync.log 2>&1
