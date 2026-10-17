# Инфраструктура

- `docker/Dockerfile` — образ Python-приложения с FFmpeg. Build context — корень репозитория; в образ копируются backend, frontend и SQL-схема.
- `nginx/` — локальный прокси, начальный ACME endpoint и production HTTPS/WebSocket.
- `compose.yaml` — локальные PostgreSQL/портал/nginx с опциональным медиашлюзом на хосте.
- `scripts/container-secrets.py` — однократная генерация секретов в корневом `.runtime/`, без вывода значений.

Из корня, при работающем Docker:

```sh
python3 infrastructure/scripts/container-secrets.py
GATEWAY_HOST=0.0.0.0 .runtime/venv/bin/python -m backend.cameras.gateway
# В другом терминале; остановите обычный портал, если он занимает 8080:
docker compose -f infrastructure/compose.yaml up --build -d
```

Шлюз защищён серверным токеном `.runtime/gateway.key`; браузеры к нему не подключаются. Для шлюза на другой машине используйте закрытую сеть и HTTPS/WSS. Автономный Linux VPS использует отдельный [production Compose](../deployment/README.md), без зависимости от Mac.

Оба Compose-файла сохраняют project name `home-cameras`, имена volumes и пути к данным в корневом `.runtime`. Перемещение Compose не создаёт новую БД. Пути bind mounts и secrets считаются от каталога соответствующего Compose-файла.
