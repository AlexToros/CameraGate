"""Administrator archive access; opaque, bounded recording references."""
import asyncio
from datetime import datetime
import hmac
import math
import secrets
import time

from aiohttp import web

from .cameras.recorder import make_recorder
from .store import Problem
from .archive_export import ArchiveExport


def search_input(data, cameras):
    cid, date, cursor=data.get('camera_id'),data.get('date'),data.get('cursor')
    if type(cid) is not int or cid not in cameras:
        raise Problem('Выберите камеру')
    try:
        day=datetime.strptime(date,'%Y-%m-%d')
        if day.strftime('%Y-%m-%d')!=date:
            raise ValueError()
        if cursor is not None:
            position=datetime.strptime(cursor,'%Y-%m-%d %H:%M:%S')
            if position.date()!=day.date():
                raise ValueError()
    except (TypeError,ValueError):
        raise Problem('Укажите дату записи') from None
    return cid,date,cursor


def recorder_error(meta):
    return Problem('У учётной записи регистратора нет доступа к архиву' if meta.get('code')=='denied'
        else 'Не удалось получить архив регистратора. Повторите попытку',403 if meta.get('code')=='denied' else 503)


class Archive:
    def __init__(self, portal):
        self.portal=portal
        self.recorder=make_recorder()
        self.entries={}
        self.searching=set()
        self.viewers={}
        self.finished={}
        self.locks={}
        self.export=ArchiveExport(self)

    async def search(self, request, data, user):
        cid,date,cursor=search_input(data,self.portal.store.snapshot['cameras'])
        config=self.portal.streams.config
        if not config:
            raise Problem('Подключение к регистратору не настроено',409)
        if self.searching:
            raise Problem('Поиск архива уже выполняется. Повторите через несколько секунд',429)
        self.searching.add(user['id'])
        try:
            result=await self.recorder.search(config,cid,date,cursor)
            self.portal.actor(request,admin=True)
            if config!=self.portal.streams.config:
                raise Problem('Регистратор изменился. Повторите поиск',409)
            if result.get('type')!='result':
                raise recorder_error(result)
            now=time.monotonic()
            # At most 2048 private references, shared across all searches. No
            # filename, device address or credentials enter browser routes.
            self.entries={key:entry for key,entry in self.entries.items() if now-entry['created']<7200}
            excess=max(0,len(self.entries)+len(result['files'])-2048)
            if excess:
                self.entries=dict(tuple(self.entries.items())[excess:])
            records=[]
            for file in result['files']:
                key=secrets.token_urlsafe(24)
                entry=dict(file,cid=cid,owner=user['id'],config=config,created=now)
                self.entries[key]=entry
                duration=(datetime.strptime(file['end'],'%Y-%m-%d %H:%M:%S')-
                    datetime.strptime(file['start'],'%Y-%m-%d %H:%M:%S')).total_seconds()
                records.append(dict(id=key,start=file['start'],end=file['end'],duration=max(0,duration)))
            return web.json_response(dict(records=records,cursor=result.get('cursor')))
        except (OSError,ValueError,RuntimeError,asyncio.TimeoutError):
            raise recorder_error({}) from None
        finally:
            self.searching.discard(user['id'])

    async def play(self, request):
        portal=self.portal
        portal.origin(request)
        user,_=portal.actor(request,admin=True)
        ws=web.WebSocketResponse(heartbeat=15,max_msg_size=4096)
        await ws.prepare(request)
        portal.sockets.add(ws)
        iterator=reading=receiving=finished=None
        try:
            data=await ws.receive_json(timeout=10)
            user,session=portal.actor(request,admin=True)
            if not isinstance(data,dict) or not hmac.compare_digest(str(data.get('csrf','')),session['csrf']):
                raise Problem('Обновите страницу',403)
            entry=self.entries.get(data.get('id'))
            if not entry or entry['owner']!=user['id'] or time.monotonic()-entry['created']>=7200:
                raise Problem('Список записей устарел. Повторите поиск',404)
            config=portal.streams.config
            if entry['config']!=config:
                raise Problem('Регистратор изменился. Повторите поиск',409)
            offset=data.get('offset',0)
            duration=(datetime.strptime(entry['end'],'%Y-%m-%d %H:%M:%S')-
                datetime.strptime(entry['start'],'%Y-%m-%d %H:%M:%S')).total_seconds()
            if type(offset) not in (int,float) or not math.isfinite(offset) or not 0<=offset<=max(0,duration):
                raise Problem('Неверное время записи')
            async with self.locks.setdefault(user['id'],asyncio.Lock()):
                previous=self.viewers.get(user['id'])
                if previous:
                    prior=self.finished[user['id']]
                    await previous.close()
                    await asyncio.wait_for(prior.wait(),4)
                portal.actor(request,admin=True)
                if len(self.viewers)>=2:
                    raise Problem('Архив уже смотрят два администратора. Повторите позже',429)
                self.viewers[user['id']]=ws
                finished=self.finished[user['id']]=asyncio.Event()
            iterator=self.recorder.records(config,dict(action='play',entry={
                key:entry[key] for key in ('file','start','end','cid')},offset=int(offset)))
            reading=asyncio.create_task(anext(iterator))
            receiving=asyncio.create_task(ws.receive())
            while not ws.closed:
                portal.actor(request,admin=True)
                if portal.streams.config!=config:
                    raise Problem('Подключение к регистратору изменилось',409)
                done,_=await asyncio.wait((reading,receiving),timeout=.5,return_when=asyncio.FIRST_COMPLETED)
                if receiving in done:
                    break
                if reading in done:
                    meta,media=reading.result()
                    if meta['type']=='error':
                        raise recorder_error(meta)
                    if meta['type']=='init':
                        await asyncio.wait_for(ws.send_json(dict(type='init',mime=meta['mime'],offset=meta.get('offset',int(offset)))),5)
                    if media:
                        await asyncio.wait_for(ws.send_bytes(media),5)
                    if meta['type']=='end':
                        await ws.send_json(dict(type='end'))
                        break
                    reading=asyncio.create_task(anext(iterator))
        except Problem as error:
            if not ws.closed:
                await ws.send_json(dict(type='error',error=str(error),retryable=error.status in (429,503)))
        except (StopAsyncIteration,ValueError,TypeError,KeyError,OSError,RuntimeError,asyncio.TimeoutError):
            if not ws.closed:
                await ws.send_json(dict(type='error',error='Не удалось воспроизвести запись. Повторите попытку',retryable=True))
        finally:
            for task in (reading,receiving):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (reading,receiving) if task),return_exceptions=True)
            if iterator:
                await iterator.aclose()
            if self.viewers.get(user['id']) is ws:
                self.viewers.pop(user['id'],None)
                self.finished.pop(user['id'],None)
            if finished:
                finished.set()
            portal.sockets.discard(ws)
            await ws.close()
        return ws
