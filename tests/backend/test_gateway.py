import asyncio
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from backend.cameras.gateway import gateway_app
from backend.cameras.gateway_client import RemotePool
from tests.backend.test_api import FakeStreams
from backend.cameras.streams import StreamPool
from backend.cameras.recorder import RemoteRecorder
from tests.backend.test_api import FakeRecorder


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_forwards_sd_and_hd_as_distinct_shared_sources(self):
        token=secrets.token_urlsafe(48);upstream=FakeStreams()
        client=TestClient(TestServer(gateway_app(token,upstream)));await client.start_server()
        try:
            ws=await client.ws_connect('/live',headers={'Authorization':'Bearer '+token})
            await ws.send_json(dict(config=dict(SERIAL_NUM='test',USERNAME='test',PASSWORD='test')))
            await ws.send_json(dict(camera_ids=[1,257],priority_camera_ids=[257],persistent_camera_ids=[1],audio_camera_ids=[257]))
            for _ in range(10):
                message=await ws.receive(timeout=3)
                if message.type==__import__('aiohttp').WSMsgType.TEXT:
                    states=__import__('json').loads(message.data).get('cameras',{})
                    if '257' in states:break
            self.assertEqual(set(states),{'1','257'})
            self.assertEqual(upstream.priority_ids,{257});self.assertEqual(upstream.persistent,{1})
            self.assertEqual(set(upstream.sources),{1,257})
            await ws.close()
        finally:await client.close()

    async def test_gateway_auth_and_multiplexed_transport(self):
        token = secrets.token_urlsafe(48)
        upstream = FakeStreams()
        upstream.motion=lambda ids:dict(available=True,camera_ids=sorted({1}.intersection(ids)))
        client = TestClient(TestServer(gateway_app(token,upstream)))
        await client.start_server()
        self.assertEqual((await client.post('/catalog',json={})).status,403)
        self.assertEqual((await client.post('/catalog',json={},headers={'Authorization':'Bearer '+token,'Origin':'https://foreign.example'})).status,403)
        config = dict(SERIAL_NUM='test-id',USERNAME='test-user',PASSWORD='test-password')
        try:
            with tempfile.TemporaryDirectory() as directory:
                key = Path(directory)/'token'
                key.write_text(token)
                with patch.dict(os.environ,dict(STREAM_GATEWAY=str(client.make_url('')).rstrip('/'),GATEWAY_TOKEN_FILE=str(key))):
                    remote = RemotePool(config)
                try:
                    self.assertEqual(await remote.catalog(),['Камера 1','Камера 2'])
                    self.assertEqual(remote.device_info,dict(name='Дом 34',channel_count=2))
                    deadline = time.monotonic()+5
                    sources = remote.subscribe([1,2,1])
                    while not all(s.frames for s in sources.values()) and time.monotonic()<deadline:
                        remote.subscribe([1,2])
                        await asyncio.sleep(.05)
                    self.assertEqual(len(sources),2)
                    self.assertTrue(all(s.frames[0][1] == upstream.image for s in sources.values()))
                    self.assertEqual(len(upstream.sources),2)
                    deadline=time.monotonic()+3
                    while not sources[1].audio and time.monotonic()<deadline:await asyncio.sleep(.02)
                    self.assertTrue(sources[1].audio)
                    self.assertTrue(sources[2].audio)
                    deadline=time.monotonic()+3
                    while not remote.motion({1})['camera_ids'] and time.monotonic()<deadline:await asyncio.sleep(.02)
                    self.assertEqual(remote.motion({1})['camera_ids'],[1])
                    self.assertEqual(remote.motion({2})['camera_ids'],[])
                    self.assertEqual(remote.subscribe([1])[1],sources[1])
                    # The portal retains both sources, but only visible cameras
                    # receive native reconnect priority across the gateway.
                    sources[2].demanded=0
                    remote.subscribe([1])
                    deadline=time.monotonic()+3
                    while getattr(upstream,'priority_ids',set())!={1} and time.monotonic()<deadline:await asyncio.sleep(.02)
                    self.assertEqual(upstream.priority_ids,{1})
                    self.assertEqual(set(upstream.sources),{1,2})
                    await remote.replace(dict(config,PASSWORD='new-test-password'))
                    self.assertFalse(remote.sources)
                    self.assertIsNone(remote.device_info)
                finally:
                    await remote.close()
        finally:
            await client.close()

    async def test_private_archive_gateway_stream_and_disconnect_cleanup(self):
        token=secrets.token_urlsafe(48);recorder=FakeRecorder()
        with patch('backend.cameras.gateway.Recorder',return_value=recorder):
            client=TestClient(TestServer(gateway_app(token,FakeStreams())))
        await client.start_server()
        config=dict(SERIAL_NUM='test-id',USERNAME='user',PASSWORD='password')
        try:
            self.assertEqual((await client.get('/archive')).status,403)
            with tempfile.TemporaryDirectory() as directory:
                key=Path(directory)/'token';key.write_text(token)
                with patch.dict(os.environ,dict(STREAM_GATEWAY=str(client.make_url('')).rstrip('/'),GATEWAY_TOKEN_FILE=str(key))):
                    remote=RemoteRecorder()
                    stream=remote.records(config,dict(action='play',entry={},offset=0))
                    meta,data=await anext(stream)
                    self.assertEqual(meta['type'],'init');self.assertEqual(data,b'init')
                    self.assertEqual((await anext(stream))[1],b'media')
                    await stream.aclose()
                    await asyncio.wait_for(recorder.closed.wait(),3)
        finally:await client.close()

    async def test_native_pool_deduplicates_viewers_and_clears_on_config_change(self):
        class FakeSource:
            def __init__(self,cid,config):
                self.cid,self.config = cid,config
                self.task = asyncio.create_task(asyncio.Event().wait())
        pool = StreamPool({'test':True})
        with patch('backend.cameras.streams.Source',FakeSource) as source:
            first = pool.subscribe([1,2,1])
            second = pool.subscribe([2,1])
            self.assertIs(first[1],second[1])
            self.assertEqual(len(pool.sources),2)
            tasks = tuple(s.task for s in pool.sources.values())
            await pool.replace({'test':'changed'})
            self.assertFalse(pool.sources)
            self.assertTrue(all(t.done() for t in tasks))


if __name__ == '__main__':
    unittest.main()
