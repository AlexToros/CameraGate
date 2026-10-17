"""Shared continuous compressed sources and bounded per-viewer fan-out."""
import asyncio
import json
import logging
import struct
import time
import uuid

from .streams import worker_command, terminate
from ..paths import ROOT

LOG = logging.getLogger('media')
RECORD = struct.Struct('!IBI')
MAX_GOP = 2*1024*1024
MAX_BATCH = 4*1024*1024
MAX_CACHE = 64*1024*1024


class EncodedSource:
    def __init__(self, cid):
        self.cid, self.token = cid, uuid.uuid4().hex
        self.touched = 0
        self.state = 'Подключение к камере…'
        self.generation, self.init, self.mime = None, b'', ''
        self.frames = []
        self.key_index, self.gop_size = 0, 0
        self.sequence, self.size, self.key_at, self.last_frame = 0, 0, 0, 0

    def reset(self, state='Соединение прервано. Повтор подключения…'):
        self.state, self.init, self.mime = state, b'', ''
        self.frames.clear()
        self.key_index = self.gop_size = 0
        self.size = self.last_frame = 0

    def feed(self, meta, data):
        kind = meta['type']
        if kind == 'status':
            self.generation = meta['generation']
            self.reset(meta['state'])
        elif kind == 'init':
            self.reset('Ожидание первого кадра…')
            self.generation = meta['generation']
            self.mime, self.init = meta['mime'], data
        elif kind == 'media' and self.init and meta['generation'] == self.generation:
            now = time.monotonic()
            if meta['key']:
                # Retain the preceding GOP so a polling viewer can still receive
                # its tail when the next keyframe arrives between two sends.
                if self.key_index:
                    self.frames = self.frames[self.key_index:]
                    self.size = self.gop_size
                self.key_index = len(self.frames)
                self.gop_size, self.key_at = 0, now
            elif not self.frames:
                return
            if self.size + len(data) > MAX_GOP or now-self.key_at > 15:
                self.frames.clear()
                self.size = 0
                self.key_index = self.gop_size = 0
                self.state = 'Ожидание ключевого кадра…'
                return
            self.sequence += 1
            self.frames.append((self.sequence, data, bool(meta['key'])))
            self.size += len(data)
            self.gop_size += len(data)
            self.last_frame, self.state = now, 'Прямой эфир'


def packet(cid, kind, data):
    return RECORD.pack(cid, kind, len(data)) + data


def pack_encoded(sources, sent):
    payload, states, total = [], {}, 0
    now = time.monotonic()
    for cid, source in sources.items():
        fresh = source.init and source.frames and now-source.last_frame < 5
        states[str(cid)] = 'Прямой эфир' if fresh else (
            'Ожидание видео…' if source.state == 'Прямой эфир' else source.state)
        if not fresh:
            sent.pop(cid, None)
            continue
        previous = sent.get(cid)
        first, last = source.frames[0][0], source.frames[-1][0]
        key = source.frames[source.key_index][0]
        reset = not previous or previous[0] != source.generation
        sequence = key-1 if reset or previous[1]<first-1 else previous[1]
        if sequence == last:
            continue
        pending = []
        if reset:
            mime = source.mime.encode('ascii')
            pending.append(packet(cid, 1, struct.pack('!H', len(mime))+mime+source.init))
        # Existing viewers get a contiguous tail across a GOP boundary. A slow
        # viewer jumps to the latest keyframe without recreating its MediaSource.
        pending.extend(packet(cid, 3 if keyframe else 2, data)
            for number, data, keyframe in source.frames[sequence-first+1:])
        size = sum(map(len, pending))
        if total+size > MAX_BATCH:
            continue
        payload.extend(pending)
        total += size
        sent[cid] = (source.generation, last)
    return states, b''.join(payload)


class RelayPool:
    def __init__(self, config=None):
        self.config = config
        self.sources, self.persistent = {}, set()
        self.changed = asyncio.Event()
        self.task = None
        self.closed = False
        self.cache_bytes = 0

    def _ensure(self, ids, touch=False):
        if not self.config or self.closed:
            return
        now = time.monotonic()
        for cid in ids:
            source = self.sources.get(cid)
            if source is None:
                self.sources[cid] = source = EncodedSource(cid)
                self.changed.set()
            if touch:
                source.touched = now
        if self.config and self.sources and (self.task is None or self.task.done()) and not self.closed:
            self.task = asyncio.create_task(self.run())

    def maintain(self, camera_ids):
        """Bulk reconciliation from the permission snapshot, no database/media I/O."""
        self.persistent = set(camera_ids)
        self._ensure(self.persistent)
        self.changed.set()

    def subscribe(self, camera_ids):
        self._ensure(camera_ids, touch=True)
        return {cid: self.sources[cid] for cid in camera_ids if cid in self.sources}

    def prune(self):
        now = time.monotonic()
        expired = [cid for cid,s in self.sources.items() if cid not in self.persistent and now-s.touched>=15]
        for cid in expired:
            source = self.sources.pop(cid)
            self.cache_bytes -= source.size+len(source.init)

    def ingest(self, meta, data):
        source = self.sources.get(meta.get('cid'))
        if source is None or meta.get('token') != source.token:
            return
        before = source.size+len(source.init)
        source.feed(meta, data)
        after = source.size+len(source.init)
        self.cache_bytes += after-before
        if self.cache_bytes > MAX_CACHE:
            self.cache_bytes -= source.size
            source.frames.clear()
            source.size = source.last_frame = 0
            source.key_index = source.gop_size = 0
            source.state = 'Достигнут лимит буфера видео. Ожидание ключевого кадра…'
        if meta['type'] == 'status' and meta.get('error'):
            LOG.warning('camera_retry cid=%d stage=%s error=%s',source.cid,
                meta.get('stage'), meta['error'])

    async def run(self):
        command = worker_command('--unused')
        command = command[:command.index('-m')] + ['-m', 'backend.cameras.relay_worker']
        delay = 1
        while self.config and not self.closed:
            if not self.sources:
                self.changed.clear()
                await self.changed.wait()
                continue
            process, writer = None, None
            try:
                process = await asyncio.create_subprocess_exec(*command, stdin=-1, stdout=-1, stderr=-3, cwd=ROOT)
                def selection():
                    return dict(camera_ids=list(self.sources), tokens={str(cid):s.token for cid,s in self.sources.items()})
                process.stdin.write((json.dumps(dict(config=self.config, **selection()))+'\n').encode())
                await process.stdin.drain()

                async def reconcile():
                    previous = selection()
                    while True:
                        self.changed.clear()
                        try:
                            await asyncio.wait_for(self.changed.wait(), 1)
                        except asyncio.TimeoutError:
                            pass
                        self.prune()
                        current = selection()
                        if current != previous:
                            process.stdin.write((json.dumps(current)+'\n').encode())
                            await process.stdin.drain()
                            previous = current
                        if not self.sources:
                            process.terminate()
                            return
                writer = asyncio.create_task(reconcile())
                while True:
                    head = await asyncio.wait_for(process.stdout.readexactly(8), 35)
                    msize, size = struct.unpack('<II', head)
                    if not 0<msize<=16384 or size>8*1024*1024:
                        raise ValueError('IPC size')
                    meta = json.loads(await asyncio.wait_for(process.stdout.readexactly(msize), 3))
                    data = await asyncio.wait_for(process.stdout.readexactly(size), 5) if size else b''
                    if meta['type'] == 'worker_error':
                        raise RuntimeError('Media worker unavailable')
                    self.ingest(meta, data)
                    delay = 1
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, KeyError, RuntimeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                LOG.warning('media_worker_restart')
            finally:
                if writer:
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
                await terminate(process)
                for source in self.sources.values():
                    source.reset()
                self.cache_bytes = 0
            await asyncio.sleep(delay)
            delay = min(30, delay*2)

    async def replace(self, config):
        self.config = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task, self.sources = None, {}
        self.cache_bytes = 0
        self.config = config
        self._ensure(self.persistent)

    async def catalog(self):
        # Catalog is one separate bulk operation; it does not start decoders.
        from .streams import StreamPool
        return await StreamPool.catalog(self)

    async def close(self):
        self.closed = True
        self.persistent.clear()
        await self.replace(None)
