# Production: bogdanova34.ru

На VPS Ubuntu x86_64 приложение находится в `/opt/video-viewer`. Управление выполняется из root-сессии. Обычному SSH-пользователю не назначены sudo или группа Docker. Исходники: публичный репозиторий `AlexToros/video-viewer`; основная ветка `main` защищена правилом PR с одобрением владельца, владелец имеет bypass.

## Сервисы и данные

`docker compose -f deployment/compose.production.yaml` запускает PostgreSQL 17, один процесс портала и nginx. Опубликованы только 80/443; приложение и БД доступны внутри Docker-сети. Заданы ограничения памяти и ротация Docker-логов. На VPS с 1 ГБ RAM короткий тест двух реальных камер прошёл; работа 30 одновременных источников на таком сервере не проверена.

- PostgreSQL: volume `home-cameras_postgres_data`.
- Ключ шифрования: `.runtime/portal-data/secret.key`; каталог принадлежит UID 10001.
- Пароль PostgreSQL: `.runtime/postgres-password`, передаётся контейнерам файлом secret.
- Linux SDK: `.runtime/libxmnetsdk.so`, readonly mount; `deployment/scripts/install-linux-sdk.sh` скачивает закреплённую ревизию и проверяет SHA-256. Путь можно переопределить через `CAMERA_SDK_PATH`.
- Рабочий P2P-узел: `.runtime/p2p-server`, readonly mount; пустой файл включает автоматическое обнаружение. Адрес узла может со временем меняться.

При первоначальном переносе сохранены пользователи, настройки доступа, камеры и личные сетки. Сессии не переносились. Для восстановления необходимы **дамп PostgreSQL и соответствующий ключ шифрования**; храните резервные копии приватно. Не выполняйте `down -v` при обновлениях.

## HTTPS

DNS: A-запись `@` → `185.22.153.61`, серверы `ns1.reg.ru`/`ns2.reg.ru`; делегирование должно появиться и в зоне `.ru`. nginx сначала использует `infrastructure/nginx/acme.conf`: публичен только HTTP challenge, остальные запросы получают 503. После выпуска сертификата `deployment/scripts/enable-https.sh` включает `infrastructure/nginx/production.conf`, HTTPS и перенаправление HTTP → HTTPS.

Первоначальный выпуск:

```sh
cd /opt/video-viewer
sh deployment/scripts/enable-https.sh
```

Сертификат находится в `/etc/letsencrypt/live/bogdanova34.ru/`. `certbot.timer` проверяет необходимость продления; deploy hook `deployment/scripts/renew-certificate.sh` проверяет nginx и выполняет reload после успешного обновления. При начальном ожидании DNS `video-viewer-https.timer` запускает проверку раз в 30 минут; после успешного включения HTTPS создаётся `.runtime/https-ready`, и service пропускается. Если выпуск прошёл, а reload завершился ошибкой, следующая попытка повторит включение HTTPS. Таймеры настроены на запуск после перезагрузки.

```sh
systemctl list-timers --all certbot.timer video-viewer-https.timer
journalctl -u video-viewer-https.service --no-pager -n 30
certbot renew --dry-run
```

`--dry-run` нужно выполнять после первоначального выпуска и появления публичного DNS. Ручной ввод DNS TXT для продления не требуется. Используется webroot-режим [Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#automated-renewals).

## Обновление и диагностика

Передайте проверенный исходный релиз в `/opt/video-viewer`, сохраняя `.runtime`, затем:

```sh
cd /opt/video-viewer
docker compose -f deployment/compose.production.yaml config --quiet
docker compose -f deployment/compose.production.yaml build app
docker compose -f deployment/compose.production.yaml up -d --wait
docker compose -f deployment/compose.production.yaml exec -T --interactive=false nginx nginx -s reload
install -m 644 deployment/systemd/initial-https.service /etc/systemd/system/video-viewer-https.service
install -m 644 deployment/systemd/initial-https.timer /etc/systemd/system/video-viewer-https.timer
install -m 755 deployment/scripts/renew-certificate.sh /etc/letsencrypt/renewal-hooks/deploy/video-viewer
systemctl daemon-reload
systemctl enable --now video-viewer-https.timer certbot.timer
docker compose -f deployment/compose.production.yaml ps
docker stats --no-stream
```

Изменение начальной SQL-схемы после этого развёртывания требует миграции. Не увеличивайте число реплик приложения: сейчас права, сессии и общий пул камер координирует один процесс.

При переходе с прежней плоской структуры особенно важно переустановить systemd units и hook указанными командами: их старые копии ссылаются на прежние пути скриптов. Project name `home-cameras`, PostgreSQL volume и корневой `.runtime` сохранены; миграция пользовательских данных для этого переноса не нужна.

`TRUST_PROXY=1` включён только в production: nginx перезаписывает `X-Real-IP`, приложение использует его для лимитов входа. Прямой доступ к порту приложения при этой настройке открывать нельзя.

Логи: `docker compose -f deployment/compose.production.yaml logs --tail=50 app`. Не включайте вывод native SDK или параметров подключения. Ни дампы, ни ключи, ни изображения камер не должны попадать в GitHub, release assets или публичные журналы.
