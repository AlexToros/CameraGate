#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec arch -x86_64 /usr/bin/python3 server.py "$@"
