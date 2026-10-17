# Домовые камеры

PWA-портал VestaCloud: вход по паролю, персональная видеостена, приглашения и управление доступом. Python/aiohttp, PostgreSQL 17 и JavaScript без фреймворка. Поддерживаются macOS и Linux x86_64.

## Структура

| Каталог | Содержимое |
| --- | --- |
| [frontend/](frontend/) | HTML, JavaScript, CSS, PWA и иконки |
| [backend/](backend/) | HTTP API, авторизация, хранилище; транспорт камер в `cameras/` |
| [database/](database/) | SQL-схема и локальный запуск PostgreSQL |
| [infrastructure/](infrastructure/) | Dockerfile, nginx, локальный Compose и подготовка секретов |
| [deployment/](deployment/) | Production Compose, HTTPS, systemd и обслуживание VPS |
| [scripts/dev/](scripts/dev/) | Установка зависимостей и запуск для разработки |
| [tests/](tests/) | Тесты API, прав доступа, хранилища и видеотранспорта |

`.runtime/` содержит локальные данные, виртуальное окружение, SDK и секреты. Этот каталог и `.env` не входят в Git и Docker build context.

## Локальный запуск

Из корня проекта:

```sh
sh scripts/dev/setup.sh  # один раз на macOS
sh scripts/dev/start.sh
```

Откройте <http://127.0.0.1:8080>. При первом запуске создаётся первый администратор; готовых паролей нет. При последующих запусках пользователи и настройки сохраняются. Требуются Python 3.14, `uv`, PostgreSQL 17 и Rosetta на Apple Silicon. Можно задать внешний `DATABASE_URL` или путь к утилитам PostgreSQL через `PG_BIN`.

Локальный PostgreSQL использует приватный Unix-сокет и данные в `.runtime/postgres`. Остановка портала — Ctrl+C; [управление БД](database/README.md). Прямой запуск бэкенда: `.runtime/venv/bin/python -m backend`.

## Возможности

- Видеостена до 64 экранов: быстрые сетки, поиск камер, изменение ширины и порядка, крупный просмотр. Настройки личные, сохраняются автоматически; пропорции видео сохраняются.
- Пользователь видит только доступные камеры. Администратор управляет пользователями, блокировкой, группами и общими названиями камер.
- Одноразовые приглашения задают имя, фамилию, роль и права заранее. Регистрация требует логин и два поля пароля; минимум 6 символов. Использованные и отозванные приглашения удаляются.
- Параметры регистратора вводятся в «Управление → Подключение», хранятся зашифрованными и не возвращаются в UI. Ключ шифрования находится отдельно от PostgreSQL.
- PWA устанавливается на телефоне; для установки требуется HTTPS. Офлайн доступна только публичная оболочка, видео и ответы API не кэшируются.

## Развёртывание и проверки

Про настройку VPS, сертификаты, сохранение данных и обновления: [deployment/README.md](deployment/README.md). Опциональный локальный контейнерный запуск с медиашлюзом: [infrastructure/README.md](infrastructure/README.md).

```sh
TEST_DATABASE_URL="postgresql://viewer@localhost:55432/video_viewer?host=$PWD/.runtime/pgsocket" \
  .runtime/venv/bin/python -m unittest discover -s tests -t . -v
node --check frontend/app.js
node --check frontend/sw.js
docker compose -f infrastructure/compose.yaml config --quiet
docker compose -f deployment/compose.production.yaml config --quiet
```

Тесты PostgreSQL создают временные схемы и не очищают рабочие таблицы. Подробнее: [tests/README.md](tests/README.md). Устройство сервера и ограничения масштабирования: [backend/README.md](backend/README.md).
