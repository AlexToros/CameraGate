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
from .relay import RelayPool, pack_encoded, pack_audio
from .recorder import Recorder

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
    recorder=Recorder()
    archive_slots=set()
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
        priority_selected=set()
        motion_sent,motion_at=None,0
        audio_selected, audio_sent = set(),{}
        initial = await ws.receive_json(timeout=10)
        await configure(initial['config'])
        config = streams.config
        async def receive():
            nonlocal selected, audio_selected, audio_sent, priority_selected
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                try:
                    values = json.loads(message.data)['camera_ids']
                    if not isinstance(values,list) or len(values)>256 or any(type(c) is not int or not 1<=c<=256 for c in values):
                        raise ValueError()
                    selected = set(values)
                    priority=json.loads(message.data).get('priority_camera_ids',values)
                    if not isinstance(priority,list) or len(priority)>256 or any(type(cid) is not int or cid not in selected for cid in priority):
                        raise ValueError()
                    priority_selected=set(priority)
                    audio = json.loads(message.data).get('audio_camera_ids',[])
                    if not isinstance(audio,list) or len(audio)>256 or any(type(cid) is not int or cid not in selected for cid in audio):
                        raise ValueError()
                    audio_selected = set(audio)
                    audio_sent = {cid:cursor for cid,cursor in audio_sent.items() if cid in audio_selected}
                    persistent = json.loads(message.data).get('persistent_camera_ids',[])
                    if not isinstance(persistent,list) or len(persistent)>256 or any(type(c) is not int or not 1<=c<=256 for c in persistent):
                        raise ValueError()
                    streams.maintain(persistent)
                except (ValueError,TypeError,KeyError):
                    await ws.close(code=4403)
        reader = asyncio.create_task(receive())
        try:
            while not ws.closed and streams.config == config:
                sources = streams.subscribe(selected,priority_ids=priority_selected)
                states,payload = pack_encoded(sources,sent)
                payload += pack_audio({cid:sources[cid] for cid in audio_selected if cid in sources},audio_sent)
                await asyncio.wait_for(ws.send_json(dict(cameras=states)),5)
                if hasattr(streams, 'motion'):
                    motion=streams.motion(selected)
                    if motion!=motion_sent or time.monotonic()-motion_at>=5:
                        await asyncio.wait_for(ws.send_json(dict(motion=motion)),5)
                        motion_sent,motion_at=motion,time.monotonic()
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
    async def archive(request):
        if len(archive_slots)>=3:
            raise web.HTTPTooManyRequests()
        ws=web.WebSocketResponse(heartbeat=15,max_msg_size=16384)
        await ws.prepare(request)
        archive_slots.add(ws);sockets.add(ws)
        stream=reading=receiving=None
        try:
            initial=await ws.receive_json(timeout=10)
            config=initial.pop('config')
            # Reuse credential validation without replacing the live pool.
            if not isinstance(config,dict) or set(config)!={'SERIAL_NUM','USERNAME','PASSWORD'} or any(not isinstance(v,str) or not 1<=len(v)<=256 for v in config.values()):
                raise ValueError()
            if initial.get('action') not in ('search','play'):
                raise ValueError()
            stream=recorder.records(config,initial)
            reading=asyncio.create_task(anext(stream));receiving=asyncio.create_task(ws.receive())
            while not ws.closed:
                done,_=await asyncio.wait((reading,receiving),return_when=asyncio.FIRST_COMPLETED)
                if receiving in done:
                    break
                meta,data=reading.result()
                await asyncio.wait_for(ws.send_json(dict(meta=meta,size=len(data))),5)
                if data:
                    await asyncio.wait_for(ws.send_bytes(data),5)
                if meta['type'] in ('result','end','error'):
                    break
                reading=asyncio.create_task(anext(stream))
        finally:
            for task in (reading,receiving):
                if task:task.cancel()
            await asyncio.gather(*(task for task in (reading,receiving) if task),return_exceptions=True)
            if stream:await stream.aclose()
            sockets.discard(ws);archive_slots.discard(ws);await ws.close()
        return ws
    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets)))
        await streams.close()
    app.router.add_post('/catalog',catalog)
    app.router.add_get('/live',live)
    app.router.add_get('/archive',archive)
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
