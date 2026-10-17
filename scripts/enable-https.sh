#!/bin/sh
# Run as root after DNS points to the VPS. Safe to retry before initial issuance.
set -eu
cd /opt/video-viewer
if [ ! -s /etc/letsencrypt/live/bogdanova34.ru/fullchain.pem ]; then
  certbot certonly --webroot --webroot-path /var/www/letsencrypt \
    --domain bogdanova34.ru --non-interactive --agree-tos --register-unsafely-without-email
fi
# Write in-place: nginx uses a bind mount of this file.
cat nginx/production.conf > .runtime/nginx.conf
docker compose -f compose.production.yaml exec -T nginx nginx -t
docker compose -f compose.production.yaml exec -T nginx nginx -s reload
install -m 755 scripts/renew-certificate.sh /etc/letsencrypt/renewal-hooks/deploy/video-viewer
systemctl enable --now certbot.timer
