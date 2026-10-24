#!/bin/sh
# Netlithic TEST node installer for Linux and macOS. Test coins have no value.
# Usage: sh install.sh   (extra options are passed to easy_node.py, e.g. --port 8001)
set -e
cd "$(dirname "$0")"
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    exec "$p" easy_node.py install "$@"
  fi
done
echo "Python 3.9 or newer is needed. Linux: install python3 and python3-venv. macOS: get it from python.org." >&2
exit 1
