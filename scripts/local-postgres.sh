#!/bin/sh
# A project-owned cluster, no launch agent and no TCP listener.
set -eu
cd "$(dirname "$0")/.."
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
if [ ! -x "$PG_BIN/pg_ctl" ]; then
  echo 'Установите PostgreSQL 17 (brew install postgresql@17) или задайте DATABASE_URL.' >&2
  exit 1
fi
mkdir -p .runtime/pgsocket
chmod 700 .runtime/pgsocket
if [ ! -f .runtime/postgres/PG_VERSION ]; then
  "$PG_BIN/initdb" -D "$PWD/.runtime/postgres" -U viewer --auth-local=trust --auth-host=scram-sha-256 --encoding=UTF8 --locale=C > .runtime/postgres-init.log
fi
if ! "$PG_BIN/pg_ctl" -D "$PWD/.runtime/postgres" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$PWD/.runtime/postgres" -l "$PWD/.runtime/postgres.log" -o "-k $PWD/.runtime/pgsocket -p 55432 -h ''" start
fi
if ! "$PG_BIN/psql" -h "$PWD/.runtime/pgsocket" -p 55432 -U viewer -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='video_viewer'" | tr -d ' ' | rg -q '^1$'; then
  "$PG_BIN/createdb" -h "$PWD/.runtime/pgsocket" -p 55432 -U viewer video_viewer
fi
