# Тесты

Python-тесты находятся в `backend/`; тестовый пакет использует production-модули через `backend.*`, без изменения sys.path.

```sh
TEST_DATABASE_URL="postgresql://viewer@localhost:55432/video_viewer?host=$PWD/.runtime/pgsocket" \
  .runtime/venv/bin/python -m unittest discover -s tests -t . -v
```

- `test_store.py`: PostgreSQL, конкурентная регистрация, приглашения, права, шифрование, сетки и bulk ACL.
- `test_api.py`: API, CSRF, cookie, PWA-файлы, авторизация/отзыв WebSocket, proxy-лимиты входа.
- `test_gateway.py`: закрытый медиашлюз и общие источники для нескольких зрителей.
- `test_protocol.py`: DVRIP, сборка пакетов, защита от старых кадров, восстановление и команды worker/FFmpeg.

PostgreSQL-проверки создают отдельные случайные схемы и удаляют только их. Без TEST_DATABASE_URL они пропускаются. Не используйте production-DSN для тестов.

Синтетические картинки проверяют UI и права, но не доказывают доступность регистратора. Эфир проверяется отдельно; изображения камер и диагностические журналы не должны попадать в git.
