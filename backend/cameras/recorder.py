"""Bounded native archive IPC, separate from the continuous live relay."""
import asyncio
import json
import struct
import os
import logging
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from .streams import worker_command, terminate
from ..paths import ROOT
LOG=logging.getLogger('archive')


class Recorder:
    async def records(self, config, request):
        command=worker_command('--unused')
        command=command[:command.index('-m')]+['-m','backend.cameras.recorder_worker']
        process=None
        try:
            process=await asyncio.create_subprocess_exec(*command,stdin=-1,stdout=-1,stderr=-3,cwd=ROOT)
            process.stdin.write((json.dumps(dict(config=config,**request))+'\n').encode())
            await process.stdin.drain()
            while True:
                try:
                    head=await asyncio.wait_for(process.stdout.readexactly(8),35)
                except asyncio.IncompleteReadError:
                    raise RuntimeError('Archive worker stopped') from None
                msize,size=struct.unpack('<II',head)
                if not 0<msize<=1024*1024 or size>8*1024*1024:
                    raise ValueError('Archive IPC size')
                meta=json.loads(await asyncio.wait_for(process.stdout.readexactly(msize),5))
                data=await asyncio.wait_for(process.stdout.readexactly(size),5) if size else b''
                if meta.get('type')=='error':
                    LOG.warning('archive_worker_error stage=%s error=%s command=%s',meta.get('stage'),meta.get('error_type'),meta.get('command'))
                yield meta,data
                if meta['type'] in ('result','end','error'):
                    return
        finally:
            await terminate(process)

    async def search(self, config, cid, date, cursor=None):
        stream=self.records(config,dict(action='search',cid=cid,date=date,cursor=cursor))
        try:
            meta,_=await anext(stream)
            return meta
        finally:
            await stream.aclose()


class RemoteRecorder(Recorder):
    async def records(self, config, request):
        url=os.environ['STREAM_GATEWAY'].rstrip('/')+'/archive'
        key=Path(os.environ.get('GATEWAY_TOKEN_FILE','.runtime/gateway.key')).read_text().strip()
        async with ClientSession(headers={'Authorization':'Bearer '+key},timeout=ClientTimeout(total=None,connect=5)) as session:
            async with session.ws_connect(url,heartbeat=15,max_msg_size=9*1024*1024) as ws:
                await ws.send_json(dict(config=config,**request))
                while True:
                    packet=await ws.receive_json(timeout=35)
                    meta,size=packet['meta'],packet['size']
                    if type(size) is not int or not 0<=size<=8*1024*1024:
                        raise ValueError('Archive gateway size')
                    data=b''
                    if size:
                        message=await ws.receive(timeout=5)
                        if message.type!=WSMsgType.BINARY or len(message.data)!=size:
                            raise ValueError('Archive gateway data')
                        data=message.data
                    yield meta,data
                    if meta['type'] in ('result','end','error'):
                        return


def make_recorder():
    return RemoteRecorder() if os.environ.get('STREAM_GATEWAY') else Recorder()
