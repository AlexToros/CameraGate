#!/bin/sh
# Certbot deploy hook: reload nginx only after a successful certificate renewal.
set -eu
cd /opt/video-viewer
docker compose -f compose.production.yaml exec -T --interactive=false nginx nginx -t
docker compose -f compose.production.yaml exec -T --interactive=false nginx nginx -s reload
