#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ ! -x .runtime/venv/bin/python ]; then
  echo 'Сначала выполните sh scripts/dev/setup.sh' >&2
  exit 1
fi
if [ -z "${DATABASE_URL:-}" ]; then
  sh database/scripts/start-local.sh
  export DATABASE_URL="postgresql://viewer@localhost:55432/video_viewer?host=$PWD/.runtime/pgsocket"
fi
exec .runtime/venv/bin/python -m backend "$@"
