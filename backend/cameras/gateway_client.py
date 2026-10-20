"""One private, authenticated multiplexed relay connection per portal."""
import asyncio
import json
import os
from pathlib import Path
import struct
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from .relay import RelayPool


class RemotePool(RelayPool):
    def __init__(self, config):
        super().__init__(config)
        self.url = os.environ['STREAM_GATEWAY'].rstrip('/')
        key = Path(os.environ.get('GATEWAY_TOKEN_FILE','.runtime/gateway.key'))
        self.headers = {'Authorization':'Bearer '+key.read_text().strip()}

    async def run(self):
        while self.config and not self.closed:
            try:
                async with ClientSession(headers=self.headers,timeout=ClientTimeout(total=None,connect=5)) as session:
                    async with session.ws_connect(self.url+'/live',heartbeat=15,max_msg_size=8*1024*1024) as ws:
                        await ws.send_json(dict(config=self.config))
                        async def subscriptions():
                            previous = None
                            while not ws.closed:
                                self.prune()
                                cutoff=time.monotonic()-2
                                current = dict(camera_ids=sorted(self.sources),persistent_camera_ids=sorted(self.persistent),audio_camera_ids=sorted(self.sources),
                                    priority_camera_ids=sorted(cid for cid,s in self.sources.items() if s.demanded>cutoff))
                                if current != previous:
                                    await ws.send_json(current)
                                    previous = current
                                await asyncio.sleep(.3)
                        sender = asyncio.create_task(subscriptions())
                        try:
                            async for message in ws:
                                if message.type == WSMsgType.TEXT:
                                    value = json.loads(message.data)
                                    if 'motion' in value:
                                        self.ingest(dict(type='motion', **value['motion']), b'')
                                        continue
                                    states = value.get('cameras',{})
                                    for cid,source in self.sources.items():
                                        state = states.get(str(cid),'Подключение…')
                                        if state != 'Прямой эфир':
                                            self.cache_bytes -= source.size+len(source.init)
                                            source.reset(state)
                                elif message.type == WSMsgType.BINARY:
                                    offset, data = 0,message.data
                                    while offset+9 <= len(data):
                                        cid,kind,size = struct.unpack_from('!IBI',data,offset)
                                        offset += 9
                                        if size>2*1024*1024+65536 or offset+size>len(data):
                                            raise ValueError('Gateway packet size')
                                        source = self.sources.get(cid)
                                        if source:
                                            payload = data[offset:offset+size]
                                            if kind == 1:
                                                length = struct.unpack_from('!H',payload)[0]
                                                self.ingest(dict(cid=cid,token=source.token,type='init',generation=uuid.uuid4().hex,
                                                    mime=payload[2:2+length].decode('ascii')),payload[2+length:])
                                            elif kind == 4:
                                                self.ingest(dict(cid=cid,token=source.token,type='audio',generation=source.generation),payload)
                                            elif kind in (2,3):
                                                self.ingest(dict(cid=cid,token=source.token,type='media',generation=source.generation,key=kind==3),payload)
                                        offset += size
                        finally:
                            sender.cancel()
                            await asyncio.gather(sender,return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.motion_ids, self.motion_at = set(), 0
                for source in self.sources.values():
                    source.reset('Сервис видео недоступен. Повтор подключения…')
                self.cache_bytes = 0
                await asyncio.sleep(2)

    async def catalog(self):
        async with ClientSession(headers=self.headers,timeout=ClientTimeout(total=50)) as session:
            async with session.post(self.url+'/catalog',json={'config':self.config}) as response:
                if response.status != 200:
                    raise RuntimeError('Gateway unavailable')
                return self.read_catalog(await response.json())
