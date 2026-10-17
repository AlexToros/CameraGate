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


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_auth_and_multiplexed_transport(self):
        token = secrets.token_urlsafe(48)
        upstream = FakeStreams()
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
                    deadline = time.monotonic()+5
                    sources = remote.subscribe([1,2,1])
                    while not all(s.jpeg for s in sources.values()) and time.monotonic()<deadline:
                        remote.subscribe([1,2])
                        await asyncio.sleep(.05)
                    self.assertEqual(len(sources),2)
                    self.assertTrue(all(s.jpeg == upstream.image for s in sources.values()))
                    self.assertEqual(len(upstream.sources),2)
                    self.assertEqual(remote.subscribe([1])[1],sources[1])
                    await remote.replace(dict(config,PASSWORD='new-test-password'))
                    self.assertFalse(remote.sources)
                finally:
                    await remote.close()
        finally:
            await client.close()

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
