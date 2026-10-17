# Локальная разработка

Бэкенд использует Python/aiohttp и PostgreSQL 17; фронтенд — JavaScript без фреймворка и сборщика. Контейнерный запуск описан в [infrastructure/](../../infrastructure/README.md), Linux-сервер — в [deployment/](../../deployment/README.md).

## macOS

Требуются Python 3.14, uv и PostgreSQL 17. На Apple Silicon транспорт использует Rosetta. Из корня репозитория:

```sh
sh scripts/dev/setup.sh
sh scripts/dev/start.sh
```

Скрипт подготовки скачивает библиотеку транспорта из дистрибутива производителя, не устанавливая само приложение. Библиотека и зависимости остаются в игнорируемом каталоге `.runtime/`.

Откройте <http://127.0.0.1:8080>. При первом запуске создайте администратора. Повторные запуски сохраняют пользователей и настройки. Остановка портала — Ctrl+C.

Без DATABASE_URL скрипт запускает локальный PostgreSQL с приватным Unix-сокетом и данными в `.runtime/postgres`. Можно задать внешний DATABASE_URL или путь к утилитам PostgreSQL через PG_BIN. [Управление БД](../../database/README.md).

Прямой запуск после подготовки окружения и настройки DATABASE_URL:

```sh
.runtime/venv/bin/python -m backend
```

## Проверки

```sh
TEST_DATABASE_URL="postgresql://viewer@localhost:55432/video_viewer?host=$PWD/.runtime/pgsocket" \
  .runtime/venv/bin/python -m unittest discover -s tests -t . -v
node --check frontend/app.js
node --check frontend/player.js
node --check frontend/sw.js
docker compose -f infrastructure/compose.yaml config --quiet
docker compose -f deployment/compose.production.yaml config --quiet
```

Тесты PostgreSQL создают временные схемы и не очищают рабочие таблицы. [Описание тестов](../../tests/README.md).

`.env`, `.runtime/`, секреты, библиотеки SDK и изображения камер не входят в Git. Локальные данные также исключены из Docker build context.
