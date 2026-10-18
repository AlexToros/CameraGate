"""Optional private macOS media worker for the Linux portal container.

It has no user-facing endpoints. A random server-to-server token is mandatory.
Native transport stays outside the web process and outside the browser.
"""
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from aiohttp import web, WSMsgType
from .relay import RelayPool, pack_encoded

from ..paths import ROOT


def gateway_app(token, pool=None):
    if len(token)<40:
        raise ValueError('Use a random gateway token of at least 40 characters')
    streams = pool or RelayPool()
    lock = asyncio.Lock()
    @web.middleware
    async def auth(request, handler):
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):
            return web.json_response({'error':'Forbidden'},status=403)
        try:
            return await handler(request)
        except (ValueError,KeyError,TypeError):
            return web.json_response({'error':'Invalid request'},status=400)
        except Exception:
            return web.json_response({'error':'Media worker unavailable'},status=503)
    app = web.Application(middlewares=[auth],client_max_size=16384)
    sockets = set()
    async def configure(value):
        if not isinstance(value,dict) or set(value) != {'SERIAL_NUM','USERNAME','PASSWORD'} or any(not isinstance(v,str) or not 1<=len(v)<=256 for v in value.values()):
            raise ValueError()
        async with lock:
            if value != streams.config:
                await streams.replace(value)
    async def catalog(request):
        data = await request.json()
        await configure(data['config'])
        channels = await streams.catalog()
        return web.json_response(dict(channels=channels, device=getattr(streams, 'device_info', None)))
    async def live(request):
        ws = web.WebSocketResponse(heartbeat=15,max_msg_size=16384)
        await ws.prepare(request)
        sockets.add(ws)
        selected, sent = set(),{}
        initial = await ws.receive_json(timeout=10)
        await configure(initial['config'])
        config = streams.config
        async def receive():
            nonlocal selected
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                try:
                    values = json.loads(message.data)['camera_ids']
                    if not isinstance(values,list) or len(values)>256 or any(type(c) is not int or not 1<=c<=256 for c in values):
                        raise ValueError()
                    selected = set(values)
                    persistent = json.loads(message.data).get('persistent_camera_ids',[])
                    if not isinstance(persistent,list) or len(persistent)>256 or any(type(c) is not int or not 1<=c<=256 for c in persistent):
                        raise ValueError()
                    streams.maintain(persistent)
                except (ValueError,TypeError,KeyError):
                    await ws.close(code=4403)
        reader = asyncio.create_task(receive())
        try:
            while not ws.closed and streams.config == config:
                states,payload = pack_encoded(streams.subscribe(selected),sent)
                await asyncio.wait_for(ws.send_json(dict(cameras=states)),5)
                if payload:
                    await asyncio.wait_for(ws.send_bytes(payload),5)
                await asyncio.sleep(.12)
        except (ConnectionError,asyncio.TimeoutError):
            pass
        finally:
            reader.cancel()
            await asyncio.gather(reader,return_exceptions=True)
            sockets.discard(ws)
            await ws.close()
        return ws
    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets)))
        await streams.close()
    app.router.add_post('/catalog',catalog)
    app.router.add_get('/live',live)
    app.on_shutdown.append(shutdown)
    return app


if __name__ == '__main__':
    key = Path(os.environ.get('GATEWAY_TOKEN_FILE',ROOT/'.runtime/gateway.key'))
    key.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd = os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,'w') as output:
            output.write(secrets.token_urlsafe(48))
    web.run_app(gateway_app(key.read_text().strip()),host=os.environ.get('GATEWAY_HOST','127.0.0.1'),port=int(os.environ.get('GATEWAY_PORT','8091')),access_log=None)
