"""One authenticated multiplexed connection from a container to its media worker."""
import asyncio
import json
import os
from pathlib import Path
import struct
import time
from types import SimpleNamespace

from aiohttp import ClientSession, ClientTimeout, WSMsgType


class RemotePool:
    def __init__(self, config):
        self.config = config
        self.url = os.environ['STREAM_GATEWAY'].rstrip('/')
        token_file = os.environ.get('GATEWAY_TOKEN_FILE','.runtime/gateway.key')
        self.headers = {'Authorization':'Bearer '+Path(token_file).read_text().strip()}
        self.sources = {}
        self.task = None

    def subscribe(self, values):
        now = time.monotonic()
        for cid in values:
            source = self.sources.get(cid)
            if source is None:
                source = self.sources[cid] = SimpleNamespace(jpeg=None,frame_id=0,last_frame=0,state='Подключение к камере…',touched=now)
            source.touched = now
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        return {cid:self.sources[cid] for cid in values}

    async def run(self):
        while True:
            try:
                async with ClientSession(headers=self.headers,timeout=ClientTimeout(total=None,connect=5)) as session:
                    async with session.ws_connect(self.url+'/live',heartbeat=15,max_msg_size=64*1024*1024) as ws:
                        await ws.send_json(dict(config=self.config,camera_ids=[]))
                        async def subscriptions():
                            previous = None
                            while not ws.closed:
                                selected = sorted(cid for cid,s in self.sources.items() if time.monotonic()-s.touched<2)
                                if selected != previous:
                                    await ws.send_json(dict(camera_ids=selected))
                                    previous = selected
                                await asyncio.sleep(.3)
                        sender = asyncio.create_task(subscriptions())
                        try:
                            async for message in ws:
                                if message.type == WSMsgType.TEXT:
                                    states = json.loads(message.data).get('cameras',{})
                                    for cid,source in self.sources.items():
                                        source.state = states.get(str(cid),'Подключение…')
                                        if source.state != 'Прямой эфир':
                                            source.jpeg = None
                                elif message.type == WSMsgType.BINARY:
                                    offset, data = 0,message.data
                                    while offset+8 <= len(data):
                                        cid,size = struct.unpack_from('!II',data,offset)
                                        offset += 8
                                        if size>8*1024*1024 or offset+size>len(data):
                                            raise ValueError('Gateway frame size')
                                        source = self.sources.get(cid)
                                        if source:
                                            source.jpeg = data[offset:offset+size]
                                            source.frame_id += 1
                                            source.last_frame = time.monotonic()
                                        offset += size
                        finally:
                            sender.cancel()
                            await asyncio.gather(sender,return_exceptions=True)
            except asyncio.CancelledError:
                return
            except Exception:
                for source in self.sources.values():
                    source.jpeg = None
                    source.state = 'Сервис видео недоступен. Повтор подключения…'
                await asyncio.sleep(2)

    async def catalog(self):
        async with ClientSession(headers=self.headers,timeout=ClientTimeout(total=50)) as session:
            async with session.post(self.url+'/catalog',json={'config':self.config}) as response:
                if response.status != 200:
                    raise RuntimeError('Gateway unavailable')
                return await response.json()

    async def replace(self, config):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.task, self.sources, self.config = None, {}, config

    async def close(self):
        await self.replace(None)
