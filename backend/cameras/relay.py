"""Shared continuous compressed sources and bounded per-viewer fan-out."""
import asyncio
from collections import deque
import json
import logging
import struct
import time
import uuid

from .audio import MAX_AUDIO
from .profiles import HD_OFFSET, source_camera
from .streams import worker_command, terminate
from ..paths import ROOT

LOG = logging.getLogger('media')
MOTION_LOG = logging.getLogger('motion')
RECORD = struct.Struct('!IBI')
MAX_GOP = 2*1024*1024
MAX_BATCH = 4*1024*1024
MAX_CACHE = 64*1024*1024


SESSION_RECOVERY_TIMEOUT = 180
SESSION_TRANSPORT_ERRORS = frozenset({'TransportClosed', 'RequestTimeout', 'TimeoutError'})


class SessionHealth:
    """A live IPC heartbeat cannot prove the recorder session is usable."""
    def __init__(self, *, clock=None, timeout=SESSION_RECOVERY_TIMEOUT):
        self.clock = clock or time.monotonic
        self.timeout = timeout
        self.started_at = self.clock()
        self.verified_at = None

    def observe(self, meta):
        # Login and acknowledged KeepAlive are the only session proofs.
        # Repeated failed logins must not extend the recovery deadline.
        if meta.get('type') == 'session' and meta.get('ready') is True:
            self.verified_at = self.clock()
        elif (meta.get('type') == 'session' and meta.get('ready') is False
              and meta.get('error') in SESSION_TRANSPORT_ERRORS):
            return 'recorder_session_transport_error'
        return None

    @property
    def expired(self):
        since = self.started_at if self.verified_at is None else self.verified_at
        return self.clock()-since >= self.timeout


class EncodedSource:
    def __init__(self, cid):
        self.cid, self.token = cid, uuid.uuid4().hex
        self.touched = self.demanded = 0
        self.state = 'Подключение к камере…'
        self.generation, self.init, self.mime = None, b'', ''
        self.frames = []
        self.key_index, self.gop_size = 0, 0
        self.sequence, self.size, self.key_at, self.last_frame = 0, 0, 0, 0
        self.audio = deque()
        self.audio_sequence = self.audio_size = 0
        self.received_frames=self.received_bytes=0
        self.arrival_gaps=0;self.max_arrival_gap=0;self.fps=0
        self.max_ipc_delay=self.max_sdk_gap=self.ipc_samples=0
        self.initializations=0;self.init_reason=None;self.max_initialization_ms=0

    def reset(self, state='Соединение прервано. Повтор подключения…'):
        self.audio.clear(); self.audio_size = 0
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
            self.initializations+=1
            self.init_reason=meta.get('reason')
            self.max_initialization_ms=max(self.max_initialization_ms,meta.get('initialization_ms',0))
            self.reset('Ожидание первого кадра…')
            self.generation = meta['generation']
            self.mime, self.init = meta['mime'], data
            self.fps=meta.get('fps',0)
        elif kind == 'audio' and self.init and meta['generation'] == self.generation:
            if not 10 < len(data) <= 8010 or data[:2] != b'\x0e\x02':
                return
            now = time.monotonic()
            self.audio_sequence += 1
            self.audio.append((self.audio_sequence, now, data))
            self.audio_size += len(data)
            while self.audio and (self.audio_size > MAX_AUDIO or now-self.audio[0][1] > 1):
                self.audio_size -= len(self.audio.popleft()[2])
        elif kind == 'media' and self.init and meta['generation'] == self.generation:
            now = time.monotonic()
            self.received_frames+=1;self.received_bytes+=len(data)
            if isinstance(meta.get('worker_at'),(int,float)):
                delay=time.time()-meta['worker_at']
                if 0<=delay<30:
                    self.ipc_samples+=1;self.max_ipc_delay=max(self.max_ipc_delay,delay)
            self.max_sdk_gap=max(self.max_sdk_gap,meta.get('sdk_gap',0))
            gap=now-self.last_frame if self.last_frame else 0
            if gap>max(.5,4/(self.fps or 12)):
                self.arrival_gaps+=1;self.max_arrival_gap=max(self.max_arrival_gap,gap)
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


def pack_audio(sources, sent):
    payload = []
    now = time.monotonic()
    for cid, source in sources.items():
        if not source.init or not source.frames or now-source.last_frame >= 5 or not source.audio or now-source.audio[-1][1] > 1:
            sent.pop(cid, None)
            continue
        previous = sent.get(cid)
        sequence = previous[1] if previous and previous[0] == source.generation else source.audio[-1][0]-1
        payload.extend(packet(cid, 4, data) for number, at, data in source.audio if number > sequence and now-at <= 1)
        sent[cid] = (source.generation, source.audio[-1][0])
    return b''.join(payload)


class RelayPool:
    def __init__(self, config=None):
        self.config = config
        self.device_info = None
        self.sources, self.persistent = {}, set()
        self.changed = asyncio.Event()
        self.task = None
        self.closed = False
        self.cache_bytes = 0
        self.motion_ids, self.motion_at = set(), 0
        self.motion_details={}
        self.motion_logged_at=0
        self.session_details={}
        self.session_health = None
        self.worker_restarts = 0
        self.last_restart_reason = None
        self.event_log_details={}

    def diagnostics(self):
        now=time.monotonic()
        return dict(worker_restarts=self.worker_restarts,last_restart_reason=self.last_restart_reason,
            session_verified_age=round(now-self.session_health.verified_at,2) if self.session_health and self.session_health.verified_at is not None else None,
            session=self.session_details.copy(),event_log=self.event_log_details.copy(),motion=dict(**self.motion_details,available=self.motion(set(self.sources))['available'],
            active_camera_ids=sorted(self.motion_ids),snapshot_age=round(now-self.motion_at,2) if self.motion_at else None),
            sources=[dict(camera_id=source_camera(cid),profile='Main' if cid>HD_OFFSET else 'Extra1',state=s.state,frame_age=round(now-s.last_frame,3) if s.last_frame else None,
                received_frames=s.received_frames,received_bytes=s.received_bytes,arrival_gaps=s.arrival_gaps,
                max_arrival_gap=round(s.max_arrival_gap,3),max_ipc_delay=round(s.max_ipc_delay,4) if s.ipc_samples else None,max_sdk_gap=round(s.max_sdk_gap,3),fps=s.fps,cache_bytes=s.size,
                initializations=s.initializations,init_reason=s.init_reason,max_initialization_ms=s.max_initialization_ms) for cid,s in self.sources.items()])

    def motion(self, camera_ids):
        available = bool(self.motion_at) and time.monotonic()-self.motion_at < 10
        return dict(available=available, camera_ids=sorted(self.motion_ids.intersection(camera_ids)) if available else [])

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

    def subscribe(self, camera_ids, *, priority_ids=None):
        self._ensure(camera_ids, touch=True)
        now=time.monotonic()
        for cid in camera_ids if priority_ids is None else priority_ids:
            if cid in self.sources:self.sources[cid].demanded=now
        return {cid: self.sources[cid] for cid in camera_ids if cid in self.sources}

    def prune(self):
        now = time.monotonic()
        expired = [cid for cid,s in self.sources.items() if cid not in self.persistent and now-s.touched>=(3 if cid>HD_OFFSET else 15)]
        for cid in expired:
            source = self.sources.pop(cid)
            self.cache_bytes -= source.size+len(source.init)

    def ingest(self, meta, data):
        if meta.get('type')=='heartbeat':
            details=meta.get('event_log',{})
            self.event_log_details={key:details[key] for key in ('written','dropped','write_failures','queued_bytes') if type(details.get(key)) is int and details[key]>=0}
            return
        if meta.get('type')=='session':
            previous=self.session_details
            if (meta.get('ready'),meta.get('generation'),meta.get('error')) != (previous.get('ready'),previous.get('generation'),previous.get('error')):
                LOG.warning('recorder_session ready=%s generation=%s error=%s',meta.get('ready'),meta.get('generation'),meta.get('error'))
            self.session_details={key:meta[key] for key in ('ready','logins','generation','error') if key in meta}
            return
        if meta.get('type') == 'motion':
            ids = meta.get('camera_ids', [])
            if isinstance(ids,list) and len(ids)<=256 and all(type(cid) is int and 1<=cid<=256 for cid in ids):
                details=meta.get('diagnostics',{})
                previous=self.motion_details
                self.motion_details={key:details[key] for key in ('notifications','motion_events','other_notifications','last_notice_id','state_checks','failures','last_event','last_event_type','last_event_channel','last_event_status','foreign_alarm_messages') if key in details}
                self.motion_details.update(method=meta.get('method'),error=meta.get('error'))
                now=time.monotonic()
                if not self.motion_logged_at or now-self.motion_logged_at>=60 or self.motion_details.get('notifications',0)!=previous.get('notifications',0) or meta.get('error')!=previous.get('error'):
                    MOTION_LOG.info('available=%s method=%s notifications=%s motion_events=%s checks=%s failures=%s active=%s kind=%s channel=%s status=%s error=%s other=%s foreign=%s',
                        bool(meta.get('available')),meta.get('method'),details.get('notifications',0),details.get('motion_events',0),details.get('state_checks',0),details.get('failures',0),
                        ','.join(map(str,sorted(ids))),details.get('last_event_type'),details.get('last_event_channel'),details.get('last_event_status'),meta.get('error'),details.get('other_notifications',0),details.get('foreign_alarm_messages',0))
                    self.motion_logged_at=now
                self.motion_ids = set(ids) if meta.get('available') else set()
                self.motion_at = time.monotonic() if meta.get('available') else 0
            return
        source = self.sources.get(meta.get('cid'))
        if source is None or meta.get('token') != source.token:
            return
        before = source.size+len(source.init)
        had_frame = bool(source.last_frame)
        source.feed(meta, data)
        if not had_frame and source.last_frame:
            LOG.warning('camera_media_ready cid=%d',source.cid)
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
            reason = 'ipc_unavailable'
            self.session_health = SessionHealth()
            try:
                process = await asyncio.create_subprocess_exec(*command, stdin=-1, stdout=-1, stderr=-3, cwd=ROOT)
                def selection():
                    cutoff=time.monotonic()-2
                    return dict(camera_ids=list(self.sources), tokens={str(cid):s.token for cid,s in self.sources.items()},
                        priority_ids=[cid for cid,s in self.sources.items() if s.demanded>cutoff])
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
                    failure = self.session_health.observe(meta)
                    self.ingest(meta, data)
                    if failure:
                        reason = failure
                        raise ConnectionError('Recorder control transport failed')
                    if self.session_health.expired:
                        reason = 'recorder_session_timeout'
                        raise TimeoutError('Recorder session recovery deadline')
                    # Heartbeats must not reset reconnect backoff either.
                    if meta.get('type') == 'session' and meta.get('ready') is True:
                        delay = 1
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, KeyError, RuntimeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                self.worker_restarts += 1
                self.last_restart_reason = reason
                LOG.warning('media_worker_restart reason=%s',reason)
            finally:
                self.event_log_details={}
                self.session_health = None
                self.session_details={}
                self.motion_ids, self.motion_at = set(), 0
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
        self.event_log_details={}
        self.motion_ids, self.motion_at = set(), 0
        self.motion_details={}
        self.session_details={}
        self.device_info = None
        self.config = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task, self.sources = None, {}
        self.cache_bytes = 0
        self.config = config
        self._ensure(self.persistent)

    def read_catalog(self, result):
        from .streams import StreamPool
        return StreamPool.read_catalog(self, result)

    async def catalog(self):
        # Catalog is one separate bulk operation; it does not start decoders.
        from .streams import StreamPool
        return await StreamPool.catalog(self)

    async def close(self):
        self.closed = True
        self.persistent.clear()
        await self.replace(None)
