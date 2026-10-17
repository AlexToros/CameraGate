#!/bin/sh
# Run as root after DNS points to the VPS. Safe to retry before initial issuance.
set -eu
cd /opt/video-viewer
if [ ! -s /etc/letsencrypt/live/bogdanova34-portal/fullchain.pem ]; then
  certbot certonly --webroot --webroot-path /var/www/letsencrypt \
    --cert-name bogdanova34-portal --domain bogdanova34.site --domain bogdanova34.ru \
    --non-interactive --agree-tos --register-unsafely-without-email
fi
# Apply the matching origin before publishing the new host. Reuses DB/data volumes.
docker compose -f deployment/compose.production.yaml up -d --wait app
# Write in-place: nginx uses a bind mount of this file.
cat infrastructure/nginx/production.conf > .runtime/nginx.conf
docker compose -f deployment/compose.production.yaml exec -T --interactive=false nginx nginx -t
docker compose -f deployment/compose.production.yaml exec -T --interactive=false nginx nginx -s reload
install -m 755 deployment/scripts/renew-certificate.sh /etc/letsencrypt/renewal-hooks/deploy/video-viewer
systemctl enable --now certbot.timer
touch .runtime/https-ready-bogdanova34-portal
