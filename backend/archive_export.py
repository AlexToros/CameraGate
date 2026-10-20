"""One bounded, temporary MP4 export; recorder/live media remain streamcopy."""
import asyncio
from datetime import datetime
from pathlib import Path
import os
import secrets
import shutil
import tempfile
import time

from aiohttp import web

from .cameras.streams import terminate
from .store import Problem

MAX_SECONDS=15*60
MAX_BYTES=256*1024*1024
FORMAT='%Y-%m-%d %H:%M:%S'


def clip_range(data, entry):
    start,end=data.get('start'),data.get('end')
    if type(start) is not int or type(end) is not int:
        raise Problem('Укажите начало и конец фрагмента')
    duration=(datetime.strptime(entry['end'],FORMAT)-datetime.strptime(entry['start'],FORMAT)).total_seconds()
    if not 0<=start<end<=duration:
        raise Problem('Фрагмент должен находиться внутри выбранной записи')
    if end-start>MAX_SECONDS:
        raise Problem('Можно скачать до 15 минут за один раз')
    return start,end


class ArchiveExport:
    def __init__(self, archive):
        self.archive=archive
        self.job=None
        self.cleanup_lock=asyncio.Lock()

    def check(self, job):
        self.archive.portal.actor(job['request'],admin=True)
        if self.archive.portal.streams.config!=job['config']:
            raise Problem('Подключение к регистратору изменилось',409)

    async def discard(self, job):
        async with self.cleanup_lock:
            await self._discard(job)

    async def _discard(self, job):
        if self.job is not job:
            return
        task=job.get('task')
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        await asyncio.to_thread(job['directory'].cleanup)
        if self.job is job:
            self.job=None

    async def maintain(self):
        try:
            while True:
                job=self.job
                if job:
                    try:
                        self.check(job)
                        age=time.monotonic()-job['touched']
                        if age>(30 if job['state']=='preparing' else 300) and not job.get('sending'):
                            await self.discard(job)
                    except Problem:
                        if job.get('sending'):
                            job['invalid']=True
                        else:
                            await self.discard(job)
                await asyncio.sleep(1)
        finally:
            if self.job:
                await self.discard(self.job)

    async def create(self, request, data, user):
        reference=data.get('id')
        entry=self.archive.entries.get(reference) if isinstance(reference,str) else None
        if not entry or entry['owner']!=user['id'] or time.monotonic()-entry['created']>=7200:
            raise Problem('Список записей устарел. Повторите поиск',404)
        if entry['config']!=self.archive.portal.streams.config:
            raise Problem('Регистратор изменился. Повторите поиск',409)
        start,end=clip_range(data,entry)
        if self.job:
            raise Problem('Другой фрагмент ещё готовится или ожидает скачивания',429)
        directory=tempfile.TemporaryDirectory(prefix='cameragate-export-')
        job=self.job=dict(id=secrets.token_urlsafe(24),owner=user['id'],request=request,config=entry['config'],
            state='preparing',touched=time.monotonic(),directory=directory,start=start,end=end,
            entry=entry,filename='camera-%s_%s.mp4' % (entry['cid'],entry['start'].replace(' ','_').replace(':','-')))
        job['task']=asyncio.create_task(self.prepare(job))
        return web.json_response(dict(id=job['id']),status=202)

    async def prepare(self, job):
        from .archive import recorder_error
        iterator=self.archive.recorder.records(job['config'],dict(action='play',entry={
            key:job['entry'][key] for key in ('file','start','end','cid')},offset=job['start'],end_offset=job['end']))
        reading=process=waiting=None
        try:
            async with asyncio.timeout(20*60):
                raw=Path(job['directory'].name)/'source.mp4'
                size=frames=0
                pending=bytearray()
                with raw.open('wb') as output:
                    reading=asyncio.create_task(anext(iterator))
                    while True:
                        self.check(job)
                        done,_=await asyncio.wait((reading,),timeout=.5)
                        if not done:
                            continue
                        meta,media=reading.result()
                        if meta['type']=='error':
                            raise recorder_error(meta)
                        if media:
                            size+=len(media)
                            if size>MAX_BYTES:
                                raise Problem('Фрагмент слишком большой. Выберите меньший интервал',413)
                            pending.extend(media)
                        if meta['type']=='media':
                            frames+=1
                        if len(pending)>=1024*1024 or meta['type']=='end':
                            await asyncio.to_thread(output.write,pending)
                            pending.clear()
                        if meta['type']=='end':
                            break
                        reading=asyncio.create_task(anext(iterator))
                await iterator.aclose()
                if not frames:
                    raise Problem('В выбранном интервале нет видео',404)
                # Finalize the ordinary MP4 index/duration without decoding.
                binary=os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
                if not binary:
                    raise RuntimeError('FFmpeg unavailable')
                target=Path(job['directory'].name)/'clip.mp4'
                process=await asyncio.create_subprocess_exec(binary,'-hide_banner','-loglevel','error','-nostdin',
                    '-threads','1','-i',str(raw),'-map','0:v:0','-an','-c:v','copy','-movflags','+faststart',
                    '-fs',str(MAX_BYTES+1024*1024),str(target),stdout=-3,stderr=-3)
                waiting=asyncio.create_task(process.wait())
                async with asyncio.timeout(30):
                    while process.returncode is None:
                        self.check(job)
                        await asyncio.wait((waiting,),timeout=.5)
                if process.returncode or not target.exists() or target.stat().st_size>=MAX_BYTES:
                    raise Problem('Не удалось собрать MP4. Выберите меньший фрагмент',503)
                raw.unlink()
                self.check(job)
                job.update(state='ready',size=target.stat().st_size,touched=time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            job.update(state='error',error=str(error) if isinstance(error,Problem) else 'Не удалось скачать запись с регистратора. Повторите попытку',touched=time.monotonic())
            await terminate(process)
            await asyncio.to_thread(job['directory'].cleanup)
        finally:
            if reading:
                reading.cancel()
                await asyncio.gather(reading,return_exceptions=True)
            await iterator.aclose()
            await terminate(process)
            if waiting:
                await waiting

    def owned(self, request, user):
        job=self.job
        if not job or job['id']!=request.match_info['export_id'] or job['owner']!=user['id']:
            raise Problem('Фрагмент недоступен. Подготовьте его заново',404)
        self.check(job)
        job['touched']=time.monotonic()
        return job

    async def status(self, request):
        portal=self.archive.portal
        portal.origin(request)
        user,_=portal.actor(request,admin=True)
        job=self.owned(request,user)
        return web.json_response(dict(state=job['state'],error=job.get('error'),size=job.get('size')))

    async def cancel(self, request):
        portal=self.archive.portal
        portal.origin(request)
        user,_=portal.actor(request,admin=True,csrf=True)
        job=self.owned(request,user)
        if job.get('sending'):
            job['invalid']=True
        else:
            await self.discard(job)
        return web.json_response(dict(ok=True))

    async def download(self, request):
        portal=self.archive.portal
        portal.origin(request)
        user,_=portal.actor(request,admin=True)
        job=self.owned(request,user)
        if job['state']!='ready' or job.get('sending'):
            raise Problem('Фрагмент ещё не готов',409)
        job['sending']=True
        response=web.StreamResponse(headers={'Content-Type':'video/mp4','Content-Length':str(job['size']),
            'Content-Disposition':'attachment; filename="%s"' % job['filename'],
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        try:
            await response.prepare(request)
            with (Path(job['directory'].name)/'clip.mp4').open('rb') as source:
                while True:
                    portal.actor(request,admin=True)
                    self.check(job)
                    if job.get('invalid'):
                        raise Problem('Скачивание отменено',409)
                    chunk=await asyncio.to_thread(source.read,256*1024)
                    if not chunk:
                        break
                    await asyncio.wait_for(response.write(chunk),10)
            await response.write_eof()
        except (Problem,ConnectionError,asyncio.TimeoutError):
            response.force_close()
            if request.transport:
                request.transport.close()
        finally:
            await self.discard(job)
        return response
