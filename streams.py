"""Shared on-demand camera producers, independent of viewers and permissions.

subscribe() is the bulk entrypoint. Each distinct camera needs its own native
session (the vendor SDK has no bulk monitor API); duplicate tiles/viewers share it.
No database or authorization queries occur in the media path.
"""
import asyncio
import json
from pathlib import Path
import struct
import sys
import time
import os

ROOT = Path(__file__).resolve().parent


def worker_command(mode, *args):
    interpreter = (['/usr/bin/arch', '-x86_64', '/usr/bin/python3']
                   if sys.platform == 'darwin' else [sys.executable])
    return [*interpreter, str(ROOT / 'vesta.py'), mode, *map(str,args)]


async def terminate(process):
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), 3)
        except (ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()


class Source:
    def __init__(self, cid, config):
        self.cid, self.config = cid, config
        self.jpeg, self.frame_id, self.last_frame = None, 0, 0
        self.state = 'Подключение к камере…'
        self.diagnostics = {}
        self.touched = time.monotonic()
        self.process = None
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            while time.monotonic() - self.touched < 15:
                try:
                    self.last_frame = 0
                    self.process = await asyncio.create_subprocess_exec(*worker_command('--worker', self.cid-1, 1),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    self.process.stdin.write((json.dumps(dict(config=self.config,channel=self.cid-1,generation=1))+'\n').encode())
                    await self.process.stdin.drain()
                    started = time.monotonic()
                    while time.monotonic() - self.touched < 15:
                        head = await asyncio.wait_for(self.process.stdout.readexactly(8), 6)
                        msize, jsize = struct.unpack('<II', head)
                        if msize > 65536 or jsize > 8*1024*1024:
                            raise ValueError('IPC frame size')
                        meta = json.loads(await asyncio.wait_for(self.process.stdout.readexactly(msize), 3))
                        jpeg = await asyncio.wait_for(self.process.stdout.readexactly(jsize), 3) if jsize else None
                        if meta['selected'] != self.cid-1 or meta['generation'] != 1:
                            continue
                        self.state = meta['state']
                        self.diagnostics = meta.get('diagnostics',{})
                        if jpeg and meta.get('frame_generation') == 1:
                            self.jpeg, self.last_frame = jpeg, time.monotonic()
                            self.frame_id += 1
                        elif not meta.get('live'):
                            self.jpeg = None
                        if time.monotonic() - (self.last_frame or started) > (12 if self.last_frame else 35):
                            raise TimeoutError()
                except (OSError, ValueError, KeyError, RuntimeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                    self.state = 'Соединение прервано. Повтор подключения…'
                finally:
                    self.jpeg = None
                    await terminate(self.process)
                    self.process = None
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass
        finally:
            self.jpeg = None
            await terminate(self.process)


class StreamPool:
    def __init__(self, config=None):
        self.config = config
        self.sources = {}
        self.gateway = None

    def subscribe(self, camera_ids):
        # Build one shared batch registry; Source tasks do their independent work
        # after this in-memory pass. There are no per-viewer camera connections.
        now = time.monotonic()
        for cid in camera_ids:
            source = self.sources.get(cid)
            if not source or source.task.done():
                source = self.sources[cid] = Source(cid, self.config)
            source.touched = now
        return {cid: self.sources[cid] for cid in camera_ids}

    async def replace(self, config):
        previous, self.sources, self.config = self.sources, {}, config
        tasks = tuple(s.task for s in previous.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def catalog(self):
        process = await asyncio.create_subprocess_exec(*worker_command('--catalog'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate((json.dumps(self.config)+'\n').encode()), 45)
            if process.returncode:
                raise RuntimeError('catalog unavailable')
            return json.loads(output)
        finally:
            await terminate(process)

    async def close(self):
        await self.replace(None)


def pack_frames(sources, sent):
    payload, states = [], {}
    now = time.monotonic()
    for cid, source in sources.items():
        fresh = source.jpeg and now-source.last_frame < 5
        states[str(cid)] = 'Прямой эфир' if fresh else source.state
        identity = (id(source),source.frame_id)
        if fresh and sent.get(cid) != identity:
            sent[cid] = identity
            payload.append(struct.pack('!II',cid,len(source.jpeg))+source.jpeg)
    return states, b''.join(payload)


def make_pool(config):
    if os.environ.get('STREAM_GATEWAY'):
        from gateway_client import RemotePool
        return RemotePool(config)
    return StreamPool(config)
