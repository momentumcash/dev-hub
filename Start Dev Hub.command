#!/bin/sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
  python3 server.py
else
  echo 'Dev Hub needs Python 3.8 or newer.'
  echo 'Get it free at https://www.python.org/downloads/ then double-click this again.'
  echo 'Press Enter to close.'
  read -r answer
fi
