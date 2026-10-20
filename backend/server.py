"""Authenticated local portal. PostgreSQL is required in every environment."""
import asyncio
from collections import defaultdict, deque
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import struct
import time
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType
from .store import Store, Problem
from .cameras.streams import make_pool
from .cameras.relay import pack_encoded, pack_audio
from .paths import ROOT, FRONTEND
from .archive import Archive
from .cameras.profiles import hd_source

COOKIE = 'viewer_session'
LOG = logging.getLogger('portal')
PORTAL = web.AppKey('portal', object)


def public_origins():
    primary = os.environ.get('PUBLIC_ORIGIN')
    if not primary:
        return []
    additional = os.environ.get('ADDITIONAL_PUBLIC_ORIGINS', '')
    return [primary, *(origin.strip() for origin in additional.split(',') if origin.strip())]


def request_origin(request):
    origins = public_origins()
    host = request.host.lower()
    return next((origin for origin in origins if urlsplit(origin).netloc.lower() == host),
                origins[0] if origins else '%s://%s' % (request.scheme, request.host))


class Portal:
    def __init__(self, store, streams, secure=False):
        self.store, self.streams, self.secure = store, streams, secure
        self.limits = defaultdict(deque)
        self.sync_lock = asyncio.Lock()
        self.sync_state = 'Список камер ещё не обновлён'
        self.connection_status = 'checking' if streams and streams.config else 'not_configured'
        self.background = set()
        self.sockets = set()
        self.archive = Archive(self)

    async def call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def background_task(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def maintain_streams(self):
        while True:
            groups = self.store.snapshot['groups']
            group = groups.get(1)
            self.streams.maintain(group['camera_ids'] if group else [])
            await asyncio.sleep(1)

    def origin(self, request):
        origin = request.headers.get('Origin')
        expected = request_origin(request)
        if origin and origin != expected:
            raise Problem('Запрос с другого сайта запрещён', 403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise Problem('Запрос с другого сайта запрещён', 403)

    def actor(self, request, admin=False, csrf=False):
        user, session = self.store.user(request.cookies.get(COOKIE))
        if admin and user['role'] != 'admin':
            raise Problem('Нужны права администратора', 403)
        if csrf and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            raise Problem('Обновите страницу и повторите запрос', 403)
        return user, session

    def throttle(self, request):
        now = time.monotonic()
        key = request.remote or 'unknown'
        # Enable only when the app is private behind a proxy that overwrites this header.
        if os.environ.get('TRUST_PROXY') == '1':
            try:
                key = str(ipaddress.ip_address(request.headers.get('X-Real-IP', '')))
            except ValueError:
                pass
        if len(self.limits) > 4096:
            self.limits = defaultdict(deque, {k:v for k,v in self.limits.items() if v and v[-1]>now-300})
        attempts = self.limits[key]
        while attempts and attempts[0] < now-300:
            attempts.popleft()
        if len(attempts) >= 20:
            raise Problem('Слишком много попыток. Повторите через пять минут', 429)
        attempts.append(now)

    def signed_in(self, token):
        response = web.json_response({'ok':True})
        response.set_cookie(COOKIE, token, httponly=True, secure=self.secure, samesite='Strict', max_age=43200, path='/')
        return response

    async def sync(self):
        if self.sync_lock.locked():
            return
        async with self.sync_lock:
            if not self.streams.config:
                self.connection_status = 'not_configured'
                self.sync_state = 'Параметры подключения не заданы'
                return
            self.connection_status = 'checking'
            self.sync_state = 'Получение списка камер…'
            try:
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        names = await self.streams.catalog()
                        break
                    except Exception:
                        if attempt >= 3:
                            raise
                        await asyncio.sleep(1)
                await self.call(self.store.sync_cameras, names)
                self.connection_status = 'connected'
                self.sync_state = 'Список камер обновлён'
            except Exception:
                self.connection_status = 'error'
                self.sync_state = 'Не удалось подключиться к регистратору. Проверьте параметры подключения и сеть'

    async def api(self, request):
        path, method = request.path, request.method
        self.origin(request)
        data = {}
        if method in ('POST','PUT','DELETE'):
            if request.content_type != 'application/json':
                raise Problem('Ожидается JSON', 415)
            try:
                data = await request.json()
            except (ValueError, UnicodeError):
                raise Problem('Неверный JSON') from None
            if not isinstance(data, dict):
                raise Problem('Ожидается JSON-объект')
        if path == '/api/bootstrap' and method == 'GET':
            return web.json_response({'setup_required':not bool(self.store.snapshot['users'])})
        if path == '/api/invite/preview' and method == 'POST':
            self.throttle(request)
            return web.json_response(await self.call(self.store.preview_invite, data.get('token')))
        auth = {'/api/auth/setup':self.store.bootstrap, '/api/auth/login':self.store.login, '/api/auth/register':self.store.register}
        if path in auth and method == 'POST':
            self.throttle(request)
            return self.signed_in(await self.call(auth[path], data))
        user, session = self.actor(request, admin=path.startswith('/api/admin/'), csrf=method != 'GET')
        if path == '/api/auth/logout' and method == 'POST':
            await self.call(self.store.logout, request.cookies.get(COOKIE,''))
            response = web.json_response({'ok':True})
            response.del_cookie(COOKIE, path='/')
            return response
        if path == '/api/me' and method == 'GET':
            return web.json_response(dict(user=self.store.public_user(user), csrf=session['csrf']))
        if path == '/api/state' and method == 'GET':
            groups = [dict(id=g['id'],name=g['name'],camera_ids=[c for c in g['camera_ids'] if user['mask'] & (1<<c)],builtin=g['builtin'])
                      for g in self.store.snapshot['groups'].values() if user['role']=='admin' or g['id'] in user['group_ids']]
            return web.json_response(dict(cameras=self.store.permitted(user),groups=groups,
                configured=bool(self.streams.config),connection_state=self.sync_state,
                layout=await self.call(self.store.layout,user)))
        if path == '/api/layout' and method == 'PUT':
            return web.json_response(await self.call(self.store.save_layout,user,data))
        if path == '/api/admin/users' and method == 'GET':
            return web.json_response({'users':[self.store.public_user(u) for u in self.store.snapshot['users'].values()]})
        if path == '/api/admin/archive/search' and method == 'POST':
            return await self.archive.search(request,data,user)
        if path == '/api/admin/archive/exports' and method == 'POST':
            return await self.archive.export.create(request,data,user)
        if path == '/api/admin/media/diagnostics' and method == 'GET':
            return web.json_response(self.streams.diagnostics() if hasattr(self.streams,'diagnostics') else {})
        if path.startswith('/api/admin/users/') and method == 'PUT':
            await self.call(self.store.edit_user,user['id'],numeric_tail(path),data)
        elif path == '/api/admin/invites' and method == 'GET':
            return web.json_response({'invitations':await self.call(self.store.invitations)})
        elif path == '/api/admin/invites' and method == 'POST':
            return web.json_response(await self.call(self.store.invite,user['id'],data))
        elif path.startswith('/api/admin/invites/') and method == 'DELETE':
            await self.call(self.store.revoke_invite,user['id'],numeric_tail(path))
        elif path == '/api/admin/groups' and method == 'POST':
            return web.json_response(await self.call(self.store.save_group,user['id'],None,data))
        elif path.startswith('/api/admin/groups/') and method == 'PUT':
            return web.json_response(await self.call(self.store.save_group,user['id'],numeric_tail(path),data))
        elif path.startswith('/api/admin/groups/') and method == 'DELETE':
            await self.call(self.store.delete_group,user['id'],numeric_tail(path))
        elif path == '/api/admin/cameras' and method == 'PUT':
            await self.call(self.store.rename_cameras,user['id'],data.get('cameras'))
        elif path == '/api/admin/connection' and method == 'PUT':
            async with self.sync_lock:
                await self.call(self.store.set_connection,user['id'],data)
                config = await self.call(self.store.connection)
                await self.streams.replace(config)
                self.connection_status = 'checking'
                self.sync_state = 'Получение списка камер…'
            self.background_task(self.sync())
        elif path == '/api/admin/connection' and method == 'GET':
            return web.json_response(dict(configured=bool(self.streams.config),state=self.sync_state,
                status=self.connection_status,device=getattr(self.streams, 'device_info', None)))
        elif path == '/api/admin/sync' and method == 'POST':
            self.background_task(self.sync())
        else:
            raise Problem('Страница не найдена',404)
        return web.json_response({'ok':True})

    async def live(self, request):
        self.origin(request)
        self.actor(request)
        ws = web.WebSocketResponse(heartbeat=20,max_msg_size=8192)
        await ws.prepare(request)
        self.sockets.add(ws)
        selected, sent, statuses = set(), {}, {}
        motion_status = None
        authenticated = False
        audio_camera = None
        hd_camera = None
        audio_hd = False
        audio_sent = {}

        async def receive():
            nonlocal selected, authenticated, audio_camera, hd_camera, audio_hd
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                try:
                    data = json.loads(message.data)
                    user, session = self.store.user(request.cookies.get(COOKIE))
                    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('csrf','')),session['csrf']):
                        raise Problem('Обновите страницу',403)
                    values = data.get('camera_ids')
                    if not isinstance(values,list) or len(values)>64 or any(type(c) is not int or c not in self.store.snapshot['cameras'] or not user['mask'] & (1<<c) for c in values):
                        raise Problem('Нет доступа к камере',403)
                    audio_id = data.get('audio_camera_id')
                    if audio_id is not None and (type(audio_id) is not int or audio_id not in values):
                        raise Problem('Нет доступа к звуку камеры',403)
                    hd_id=data.get('hd_camera_id')
                    if hd_id is not None and (type(hd_id) is not int or hd_id not in values):
                        raise Problem('Нет доступа к HD камеры',403)
                    use_hd=data.get('audio_hd',False)
                    if type(use_hd) is not bool or (use_hd and (audio_id is None or audio_id!=hd_id)):
                        raise Problem('Нет доступа к звуку камеры',403)
                    if audio_id != audio_camera or use_hd!=audio_hd:
                        audio_sent.clear()
                    audio_camera = audio_id
                    hd_camera,audio_hd=hd_id,use_hd
                    selected = set(values)
                    source_ids=selected | ({hd_source(hd_camera)} if hd_camera is not None else set())
                    for cid in sent.keys() - source_ids:
                        sent.pop(cid)
                    authenticated = True
                except (ValueError, TypeError, Problem):
                    await ws.close(code=4403,message='Нет доступа'.encode())
                    break

        receiver = asyncio.create_task(receive())
        try:
            started = time.monotonic()
            while not ws.closed:
                try:
                    user, _ = self.store.user(request.cookies.get(COOKIE))
                except Problem:
                    await ws.close(code=4401,message='Войдите снова'.encode())
                    break
                denied = {cid for cid in selected if cid not in self.store.snapshot['cameras'] or not user['mask'] & (1<<cid)}
                if denied:
                    selected -= denied
                    if audio_camera in denied:
                        audio_camera = None; audio_sent.clear()
                    if hd_camera in denied:
                        hd_camera=None;audio_hd=False
                    await ws.send_json(dict(type='revoked',camera_ids=sorted(denied)))
                if not authenticated and time.monotonic()-started>10:
                    await ws.close(code=4403)
                    break
                if authenticated and self.streams.config:
                    source_ids=selected | ({hd_source(hd_camera)} if hd_camera is not None else set())
                    sources = self.streams.subscribe(source_ids)
                    # In-memory traversal only. The socket emits one bounded batch
                    # of latest frames; slow readers never accumulate video history.
                    state, payload = pack_encoded(sources,sent)
                    audio_source=hd_source(audio_camera) if audio_hd and audio_camera is not None else audio_camera
                    if audio_source in sources:
                        payload += pack_audio({audio_source:sources[audio_source]},audio_sent)
                    if state != statuses:
                        statuses = state
                        await asyncio.wait_for(ws.send_json(dict(type='status',cameras=state)),5)
                    if payload:
                        await asyncio.wait_for(ws.send_bytes(payload),5)
                    if hasattr(self.streams, 'motion'):
                        motion = self.streams.motion(selected)
                        if motion != motion_status:
                            motion_status = motion
                            await asyncio.wait_for(ws.send_json(dict(type='motion', **motion)),5)
                elif authenticated:
                    state = {str(cid):'Подключение не настроено. Обратитесь к администратору' for cid in selected}
                    if state != statuses:
                        statuses = state
                        await ws.send_json(dict(type='status',cameras=state))
                await asyncio.sleep(.05)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            pass
        finally:
            receiver.cancel()
            await asyncio.gather(receiver,return_exceptions=True)
            self.sockets.discard(ws)
            await ws.close()
        return ws


def numeric_tail(path):
    try:
        value = int(path.rsplit('/',1)[-1])
        if value < 1:
            raise ValueError()
        return value
    except ValueError:
        raise Problem('Неверный идентификатор') from None


@web.middleware
async def errors(request, handler):
    try:
        allowed = {'localhost','127.0.0.1','::1'}
        allowed.update(urlsplit(origin).hostname for origin in public_origins())
        if urlsplit('//'+request.host).hostname not in allowed:
            raise Problem('Неизвестный адрес сервиса',403)
        response = await handler(request)
    except Problem as error:
        response = web.json_response({'error':str(error)},status=error.status)
    except web.HTTPException as error:
        response = web.json_response({'error':'Страница не найдена' if error.status==404 else error.reason},status=error.status)
    except Exception:
        # Deliberately omit exception text/traceback: upstream errors may contain secrets.
        LOG.error('request_failed method=%s path=%s',request.method,request.path)
        response = web.json_response({'error':'Не удалось выполнить запрос. Повторите позже'},status=500)
    if not response.prepared:
        public = urlsplit(request_origin(request))
        websocket_origin = ('wss' if public.scheme=='https' else 'ws')+'://'+public.netloc
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' %s; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" % websocket_origin})
    return response


def create_app(store, streams=None, secure=False, auto_sync=True):
    portal = Portal(store, streams or make_pool(store.connection()),secure)
    app = web.Application(middlewares=[errors],client_max_size=256*1024)
    app[PORTAL] = portal
    app.router.add_get('/api/live',portal.live)
    app.router.add_get('/api/admin/archive/play',portal.archive.play)
    app.router.add_get('/api/admin/archive/exports/{export_id}',portal.archive.export.status)
    app.router.add_delete('/api/admin/archive/exports/{export_id}',portal.archive.export.cancel)
    app.router.add_get('/api/admin/archive/exports/{export_id}/download',portal.archive.export.download,allow_head=False)
    app.router.add_route('*','/api/{tail:.*}',portal.api)
    assets = {'/':'index.html','/app.js':'app.js','/admin.js':'admin.js','/archive.js':'archive.js','/reorder.js':'reorder.js','/audio.js':'audio.js','/player.js':'player.js','/style.css':'style.css','/manifest.webmanifest':'manifest.webmanifest',
              '/sw.js':'sw.js','/icon-192.png':'icons/icon-192.png','/icon-512.png':'icons/icon-512.png','/apple-touch-icon.png':'icons/apple-touch-icon.png'}
    async def asset(request):
        return web.FileResponse(FRONTEND/assets[request.path])
    app.router.add_get('/',asset)
    for path in assets.keys()-{'/'}:
        app.router.add_get(path,asset)
    async def health(request):
        await asyncio.to_thread(store.refresh)
        return web.json_response({'ok':True})
    # Healthchecks do not return configuration, identity or media.
    app.router.add_get('/health',health)
    async def startup(app):
        portal.background_task(portal.maintain_streams())
        portal.background_task(portal.archive.export.maintain())
        if auto_sync:
            portal.background_task(portal.sync())
    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(portal.sockets)))
        for task in tuple(portal.background):
            task.cancel()
        await asyncio.gather(*portal.background,return_exceptions=True)
        await portal.streams.close()
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    return app


def main():
    data_dir = Path(os.environ.get('DATA_DIR',ROOT/'.runtime/data'))
    data_dir.mkdir(parents=True,exist_ok=True)
    motion_log=logging.getLogger('motion')
    motion_handler=RotatingFileHandler(data_dir/'motion.log',maxBytes=256*1024,backupCount=2,encoding='utf-8')
    motion_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    motion_log.addHandler(motion_handler);motion_log.setLevel(logging.INFO);motion_log.propagate=False
    dsn = os.environ.get('DATABASE_URL','postgresql://viewer@localhost:55432/video_viewer?host='+str(ROOT/'.runtime/pgsocket'))
    if os.environ.get('DB_PASSWORD_FILE'):
        from psycopg.conninfo import make_conninfo
        dsn = make_conninfo(dsn,password=Path(os.environ['DB_PASSWORD_FILE']).read_text().strip())
    store = Store(dsn,data_dir)
    store.import_legacy()
    app = create_app(store,secure=os.environ.get('COOKIE_SECURE')=='1')
    try:
        web.run_app(app,host=os.environ.get('LISTEN_HOST','127.0.0.1'),port=int(os.environ.get('VIEWER_PORT','8080')),access_log=None)
    finally:
        store.close()


if __name__ == '__main__':
    main()
