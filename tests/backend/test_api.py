import asyncio
import struct
from contextlib import chdir
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import CookieJar, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from backend.server import create_app, COOKIE, Portal, PORTAL
from backend.store import Problem
from backend.paths import FRONTEND
from backend.cameras.relay import EncodedSource
from tests.backend.test_store import DatabaseCase, PASSWORD


class ProxyThrottleTests(unittest.TestCase):
    def test_proxy_clients_have_independent_limits(self):
        portal = Portal(None, None)
        request = SimpleNamespace(remote='172.20.0.3', headers={'X-Real-IP':'192.0.2.1'})
        with patch.dict('os.environ', {'TRUST_PROXY':'1'}):
            for _ in range(20):
                portal.throttle(request)
            with self.assertRaises(Problem):
                portal.throttle(request)
            request.headers['X-Real-IP'] = '192.0.2.2'
            portal.throttle(request)
        self.assertEqual(len(portal.limits), 2)

    def test_untrusted_or_invalid_forwarded_address_cannot_bypass_limit(self):
        for trusted in ('0', '1'):
            with self.subTest(trusted=trusted), patch.dict('os.environ', {'TRUST_PROXY':trusted}):
                portal = Portal(None, None)
                request = SimpleNamespace(remote='172.20.0.3', headers={})
                for index in range(20):
                    request.headers['X-Real-IP'] = f'192.0.2.{index}' if trusted == '0' else f'invalid-{index}'
                    portal.throttle(request)
                with self.assertRaises(Problem):
                    portal.throttle(request)


class FakeStreams:
    def __init__(self):
        self.config = None
        self.device_info = None
        self.sources = {}
        self.image = (FRONTEND/'icons/icon-192.png').read_bytes()
        self.persistent = set()
    def maintain(self, values):
        self.persistent = set(values)
    def subscribe(self, values, *, priority_ids=None):
        self.priority_ids=set(values if priority_ids is None else priority_ids)
        for cid in values:
            if cid not in self.sources:
                source = self.sources[cid] = EncodedSource(cid)
                source.feed(dict(type='init',generation='test',mime='video/mp4; codecs="avc1.42E01E"'),b'init')
                source.feed(dict(type='media',generation='test',key=True),self.image)
                source.feed(dict(type='audio',generation='test'),struct.pack('!BBd',14,2,0)+b'\xd5'*320)
            self.sources[cid].last_frame = time.monotonic()
        return {cid:self.sources[cid] for cid in values}
    async def replace(self, config):
        self.config = config
        self.device_info = None
        self.sources = {}
    async def catalog(self):
        self.device_info = dict(name='Дом 34',channel_count=2)
        return ['Камера 1','Камера 2']
    async def close(self):
        pass


class FakeRecorder:
    def __init__(self):
        self.search_calls=[];self.play_calls=[];self.closed=asyncio.Event()
    async def search(self,config,cid,date,cursor):
        self.search_calls.append((cid,date,cursor))
        return dict(type='result',files=[dict(file='/private/channel-record.h264',start=date+' 00:00:00',end=date+' 01:00:00')],cursor=None)
    async def records(self,config,request):
        self.play_calls.append(request)
        try:
            yield dict(type='init',mime='video/mp4; codecs="avc1.42E01E"'),b'init'
            yield dict(type='media'),b'media'
            await asyncio.Event().wait()
        finally:self.closed.set()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(DatabaseCase.setUpClass.__func__)
    tearDownClass = classmethod(DatabaseCase.tearDownClass.__func__)
    setUp = DatabaseCase.setUp
    tearDown = DatabaseCase.tearDown

    async def asyncSetUp(self):
        self.streams = FakeStreams()
        self.app = create_app(self.store,self.streams,auto_sync=False)
        self.client = TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        response = await self.client.post('/api/auth/setup',json=dict(login='admin',password=PASSWORD,first_name='A',last_name='B'))
        self.assertEqual(response.status,200)
        self.me = await (await self.client.get('/api/me')).json()
        cookie = next(c.value for c in self.client.session.cookie_jar if c.key == COOKIE)
        self.client.session.cookie_jar.clear()
        self.client.session.headers['Cookie'] = COOKIE+'='+cookie
        self.headers = {'X-CSRF-Token':self.me['csrf']}
        self.store.sync_cameras(['One','Two'])

    async def asyncTearDown(self):
        await self.client.close()

    async def viewer(self):
        invite = self.store.invite(self.me['user']['id'],dict(first_name='User',last_name='Viewer',role='user',camera_ids=[1]))
        token = self.store.register(dict(login='viewer',password=PASSWORD,token=invite['token']))
        user,session = self.store.user(token)
        return user, {'Cookie':COOKIE+'='+token,'X-CSRF-Token':session['csrf']},session['csrf']

    async def test_archive_requires_admin_csrf_and_private_record_reference(self):
        archive=self.app[PORTAL].archive;recorder=archive.recorder=FakeRecorder()
        self.streams.config={'test':True}
        _,headers,_=await self.viewer()
        query=dict(camera_id=1,date='2026-10-20')
        self.assertEqual((await self.client.post('/api/admin/archive/search',headers=headers,json=query)).status,403)
        self.assertEqual((await self.client.post('/api/admin/archive/search',json=query)).status,403)
        self.assertEqual(recorder.search_calls,[])
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/api/admin/archive/play',headers=headers)
        response=await self.client.post('/api/admin/archive/search',headers=self.headers,json=query)
        self.assertEqual(response.status,200)
        result=await response.json();record=result['records'][0]
        self.assertNotIn('/private',str(result));self.assertEqual(record['duration'],3600)
        self.assertEqual(recorder.search_calls,[(1,'2026-10-20',None)])
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf='wrong',id=record['id']))
        invalid=await ws.receive_json()
        self.assertEqual(invalid['type'],'error');self.assertFalse(invalid['retryable'])
        await ws.close();self.assertEqual(recorder.play_calls,[])
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf=self.me['csrf'],id='/private/channel-record.h264'))
        self.assertEqual((await ws.receive_json())['type'],'error')
        await ws.close();self.assertEqual(recorder.play_calls,[])

    async def test_archive_transient_errors_retry_but_recorder_denial_is_terminal(self):
        class FailingRecorder(FakeRecorder):
            code='unavailable'
            async def records(self,config,request):
                yield dict(type='error',code=self.code),b''
        recorder=self.app[PORTAL].archive.recorder=FailingRecorder()
        self.streams.config={'test':True}
        response=await self.client.post('/api/admin/archive/search',headers=self.headers,json=dict(camera_id=1,date='2026-10-20'))
        record=(await response.json())['records'][0]
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf=self.me['csrf'],id=record['id']))
        self.assertTrue((await ws.receive_json(timeout=3))['retryable'])
        await ws.close()
        recorder.code='denied'
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf=self.me['csrf'],id=record['id']))
        self.assertFalse((await ws.receive_json(timeout=3))['retryable'])
        await ws.close()

    async def test_archive_stream_revokes_role_and_cleans_up_native_reader(self):
        archive=self.app[PORTAL].archive;recorder=archive.recorder=FakeRecorder()
        self.streams.config={'test':True}
        response=await self.client.post('/api/admin/archive/search',headers=self.headers,json=dict(camera_id=2,date='2026-10-20'))
        record=(await response.json())['records'][0]
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf=self.me['csrf'],id=record['id'],offset=120))
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'init')
        self.assertEqual((await ws.receive(timeout=3)).data,b'init')
        self.assertEqual((await ws.receive(timeout=3)).data,b'media')
        self.assertEqual(recorder.play_calls[0]['entry']['cid'],2)
        self.assertEqual(recorder.play_calls[0]['offset'],120)
        invite=self.store.invite(self.me['user']['id'],dict(first_name='Admin',last_name='Other',role='admin'))
        token=self.store.register(dict(login='otheradmin',password=PASSWORD,token=invite['token']))
        other,_=self.store.user(token)
        original=self.store.snapshot['users'][self.me['user']['id']]
        self.store.edit_user(other['id'],original['id'],dict(self.store.public_user(original),role='user',camera_ids=[1],group_ids=[]))
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'error')
        await ws.receive(timeout=3)
        await asyncio.wait_for(recorder.closed.wait(),3)
        self.assertEqual(archive.viewers,{})
        await ws.close()

    async def test_motion_messages_only_include_authorized_subscribed_cameras(self):
        self.streams.config={'test':True}
        moving={1,2}
        self.streams.motion=lambda ids:dict(available=True,camera_ids=sorted(moving.intersection(ids)))
        _,headers,csrf=await self.viewer()
        ws=await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[1]))
        await ws.receive(timeout=3);await ws.receive(timeout=3)
        self.assertEqual(await ws.receive_json(timeout=3),dict(type='motion',available=True,camera_ids=[1]))
        moving.clear()
        self.assertEqual(await ws.receive_json(timeout=3),dict(type='motion',available=True,camera_ids=[]))
        await ws.close()

    async def test_archive_references_are_owner_bound_and_config_changes_stop_playback(self):
        archive=self.app[PORTAL].archive;recorder=archive.recorder=FakeRecorder()
        self.streams.config={'test':True}
        response=await self.client.post('/api/admin/archive/search',headers=self.headers,json=dict(camera_id=1,date='2026-10-20'))
        record=(await response.json())['records'][0]
        invite=self.store.invite(self.me['user']['id'],dict(first_name='Admin',last_name='Second',role='admin'))
        token=self.store.register(dict(login='secondadmin',password=PASSWORD,token=invite['token']))
        _,session=self.store.user(token)
        other=await self.client.ws_connect('/api/admin/archive/play',headers={'Cookie':COOKIE+'='+token})
        await other.send_json(dict(csrf=session['csrf'],id=record['id']))
        self.assertEqual((await other.receive_json(timeout=3))['type'],'error');await other.close()
        self.assertEqual(recorder.play_calls,[])
        ws=await self.client.ws_connect('/api/admin/archive/play')
        await ws.send_json(dict(csrf=self.me['csrf'],id=record['id']))
        await ws.receive_json(timeout=3);await ws.receive(timeout=3);await ws.receive(timeout=3)
        self.streams.config={'test':'changed'}
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'error')
        await asyncio.wait_for(recorder.closed.wait(),3);await ws.close()

    async def test_replacing_archive_waits_for_previous_reader_cleanup(self):
        class SlowRecorder(FakeRecorder):
            active=peak=0
            async def records(self,config,request):
                self.active+=1;self.peak=max(self.peak,self.active)
                try:
                    yield dict(type='init',mime='video/mp4'),b'init'
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(.05)
                    self.active-=1
        archive=self.app[PORTAL].archive;recorder=archive.recorder=SlowRecorder()
        self.streams.config={'test':True}
        response=await self.client.post('/api/admin/archive/search',headers=self.headers,json=dict(camera_id=1,date='2026-10-20'))
        record=(await response.json())['records'][0]
        first=await self.client.ws_connect('/api/admin/archive/play')
        await first.send_json(dict(csrf=self.me['csrf'],id=record['id']))
        await first.receive_json(timeout=3);await first.receive(timeout=3)
        second=await self.client.ws_connect('/api/admin/archive/play')
        await second.send_json(dict(csrf=self.me['csrf'],id=record['id'],offset=10))
        self.assertEqual((await second.receive_json(timeout=3))['type'],'init')
        self.assertEqual(recorder.peak,1)
        await first.close();await second.close()
        for _ in range(40):
            if not recorder.active:break
            await asyncio.sleep(.02)
        self.assertEqual(recorder.active,0)

    async def test_csrf_roles_private_files_and_write_only_credentials(self):
        for path in ('/.env','/.runtime/data/secret.key','/store.py','/backend/store.py','/database/schema.sql','/frontend/README.md'):
            self.assertEqual((await self.client.get(path)).status,404)
        self.assertEqual((await self.client.put('/api/admin/connection',json={},headers={'Origin':'https://evil.example'})).status,403)
        self.assertEqual((await self.client.put('/api/admin/connection',json={})).status,403)
        _,headers,_ = await self.viewer()
        self.assertEqual((await self.client.get('/api/admin/media/diagnostics',headers=headers)).status,403)
        self.assertEqual((await self.client.get('/api/admin/users',headers=headers)).status,403)
        state = await (await self.client.get('/api/state',headers=headers)).json()
        self.assertEqual([c['id'] for c in state['cameras']],[1])
        self.assertFalse(state['configured'])
        secret = dict(ID='sensitive-id',UserName='sensitive-user',Password='sensitive-password')
        self.assertEqual((await self.client.put('/api/admin/connection',json=secret,headers=self.headers)).status,200)
        for path in ('/api/admin/connection','/api/state','/api/me','/api/admin/users'):
            body = await (await self.client.get(path)).text()
            for value in secret.values():
                self.assertNotIn(value,body)

    async def test_connection_summary_tracks_success_failure_and_credential_replacement(self):
        portal=self.app[PORTAL]
        initial=await (await self.client.get('/api/admin/connection')).json()
        self.assertEqual(initial['status'],'not_configured')
        self.assertIsNone(initial['device'])
        self.streams.config=dict(SERIAL_NUM='private-id',USERNAME='private-user',PASSWORD='private-password')
        await portal.sync()
        summary=await (await self.client.get('/api/admin/connection')).json()
        self.assertEqual(summary['status'],'connected')
        self.assertEqual(summary['device'],dict(name='Дом 34',channel_count=2))
        with patch.object(self.streams,'catalog',side_effect=RuntimeError('unavailable')):
            await portal.sync()
        failed=await (await self.client.get('/api/admin/connection')).json()
        self.assertEqual(failed['status'],'error')
        # Keep the new credentials unverified until their own catalog succeeds.
        gate=asyncio.Event()
        async def delayed_catalog():
            await gate.wait()
            return await FakeStreams.catalog(self.streams)
        with patch.object(self.streams,'catalog',side_effect=delayed_catalog):
            response=await self.client.put('/api/admin/connection',json=dict(ID='new-id',UserName='new-user',Password='new-password'),headers=self.headers)
            self.assertEqual(response.status,200)
            changed=await (await self.client.get('/api/admin/connection')).json()
            self.assertEqual(changed['status'],'checking')
            self.assertIsNone(changed['device'])
            gate.set()
            await asyncio.gather(*(task for task in tuple(portal.background) if task.get_coro().__name__=='sync'))
        _,headers,_=await self.viewer()
        self.assertEqual((await self.client.get('/api/admin/connection',headers=headers)).status,403)

    async def test_audio_opt_in_is_single_camera_scoped_and_cannot_bypass_grants(self):
        from tests.backend.test_relay import records
        _,headers,csrf=await self.viewer()
        self.streams.config={'test':True}
        ws=await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[1]))
        await ws.receive_json(timeout=3)
        first=await ws.receive(timeout=3)
        self.assertNotIn(4,[kind for cid,kind,data in records(first.data)])
        await ws.send_json(dict(csrf=csrf,camera_ids=[1],audio_camera_id=1))
        audio=await ws.receive(timeout=3)
        self.assertEqual([(cid,kind) for cid,kind,data in records(audio.data)],[(1,4)])
        await ws.send_json(dict(csrf=csrf,camera_ids=[1],audio_camera_id=None))
        await asyncio.sleep(.12)
        self.streams.sources[1].feed(dict(type='audio',generation='test'),struct.pack('!BBd',14,2,.04)+b'\x55'*320)
        with self.assertRaises(asyncio.TimeoutError):await ws.receive(timeout=.2)
        await ws.close()
        forbidden=await self.client.ws_connect('/api/live',headers=headers)
        await forbidden.send_json(dict(csrf=csrf,camera_ids=[1],audio_camera_id=2))
        await forbidden.receive(timeout=3)
        self.assertEqual(forbidden.close_code,4403)

    async def test_active_stream_revocation_and_block(self):
        user,headers,csrf = await self.viewer()
        self.streams.config = {'test':True}
        ws = await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[1],audio_camera_id=1))
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type,WSMsgType.TEXT)
        self.assertEqual((await ws.receive(timeout=3)).type,WSMsgType.BINARY)
        data = dict(self.store.public_user(user),camera_ids=[],blocked=False)
        self.store.edit_user(self.me['user']['id'],user['id'],data)
        message = await ws.receive_json(timeout=3)
        self.assertEqual(message,dict(type='revoked',camera_ids=[1]))
        self.store.edit_user(self.me['user']['id'],user['id'],dict(data,blocked=True))
        message = await ws.receive(timeout=3)
        while message.type == WSMsgType.TEXT:
            self.assertNotIn('1',message.json().get('cameras',{}))
            message = await ws.receive(timeout=3)
        self.assertEqual(message.type,WSMsgType.CLOSE)
        self.assertEqual(ws.close_code,4401)
        self.assertEqual((await self.client.get('/api/state',headers=headers)).status,401)

    async def test_all_group_edit_revokes_stream_and_keeps_full_admin_catalog(self):
        user,headers,csrf = await self.viewer()
        admin_id = self.me['user']['id']
        self.store.edit_user(admin_id,user['id'],dict(self.store.public_user(user),camera_ids=[],group_ids=[1],blocked=False))
        self.streams.config = {'test':True}
        ws = await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[2]))
        self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.TEXT)
        self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.BINARY)
        response = await self.client.put('/api/admin/groups/1',json=dict(name='Все камеры',camera_ids=[1]),headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual(await ws.receive_json(timeout=3),dict(type='revoked',camera_ids=[2]))
        admin_state = await (await self.client.get('/api/state')).json()
        self.assertEqual([c['id'] for c in admin_state['cameras']], [1,2])
        self.assertEqual(admin_state['groups'][0]['camera_ids'], [1])
        viewer_state = await (await self.client.get('/api/state',headers=headers)).json()
        self.assertEqual([c['id'] for c in viewer_state['cameras']], [1])
        await ws.close()

    async def test_websocket_cannot_request_ungranted_camera(self):
        _,headers,csrf = await self.viewer()
        self.streams.config = {'test':True}
        ws = await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[2]))
        self.assertEqual((await ws.receive(timeout=3)).type,WSMsgType.CLOSE)
        self.assertEqual(ws.close_code,4403)
        self.assertFalse(self.streams.sources)

    async def test_missing_connection_status_and_shared_camera(self):
        _,headers,csrf = await self.viewer()
        ws = await self.client.ws_connect('/api/live',headers=headers)
        await ws.send_json(dict(csrf=csrf,camera_ids=[1],audio_camera_id=1))
        message = await ws.receive_json(timeout=3)
        self.assertIn('не настроено',message['cameras']['1'])
        self.streams.config = {'test':True}
        while (await ws.receive(timeout=3)).type != WSMsgType.BINARY:
            pass
        other = await self.client.ws_connect('/api/live',headers=headers)
        await other.send_json(dict(csrf=csrf,camera_ids=[1,1]))
        while (await other.receive(timeout=3)).type != WSMsgType.BINARY:
            pass
        self.assertEqual(len(self.streams.sources),1)
        await ws.close()
        await other.close()

    async def test_pwa_shell_and_cookie_flags(self):
        response = await self.client.get('/manifest.webmanifest')
        manifest = await response.json(content_type=None)
        self.assertEqual(manifest['display'],'standalone')
        for path in ('/','/app.js','/admin.js','/audio.js','/player.js','/style.css','/sw.js','/icon-192.png','/icon-512.png','/apple-touch-icon.png'):
            self.assertEqual((await self.client.get(path)).status,200)
        response = await self.client.post('/api/auth/login',json=dict(login='admin',password=PASSWORD))
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'],'Strict')

    async def test_continuous_camera_set_tracks_builtin_edits_without_viewers(self):
        async def wait_for(ids):
            for _ in range(40):
                if self.streams.persistent == set(ids):
                    return
                await asyncio.sleep(.05)
            self.fail('Continuous set was not reconciled')
        await wait_for([1,2])
        response=await self.client.put('/api/admin/groups/1',headers=self.headers,
            json=dict(name='Все камеры',camera_ids=[2]))
        self.assertEqual(response.status,200)
        await wait_for([2])
        response=await self.client.put('/api/admin/groups/1',headers=self.headers,
            json=dict(name='Все камеры',camera_ids=[]))
        self.assertEqual(response.status,200)
        await wait_for([])

    async def test_frontend_paths_do_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            response = await self.client.get('/')
            self.assertEqual(response.status, 200)
            self.assertIn('/app.js', await response.text())
            response = await self.client.get('/icon-192.png')
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.read()).startswith(b'\x89PNG'))

    async def test_both_domains_have_matching_csp_and_strict_origin_checks(self):
        config = {'PUBLIC_ORIGIN':'https://bogdanova34.site',
                  'ADDITIONAL_PUBLIC_ORIGINS':'https://bogdanova34.ru'}
        with patch.dict('os.environ', config):
            for host in ('bogdanova34.site', 'bogdanova34.ru'):
                response = await self.client.get('/api/me', headers={'Host':host, 'Origin':'https://'+host})
                self.assertEqual(response.status, 200)
                self.assertIn('wss://'+host, response.headers['Content-Security-Policy'])
            response = await self.client.get('/api/me', headers={
                'Host':'bogdanova34.ru', 'Origin':'https://bogdanova34.site'})
            self.assertEqual(response.status, 403)
            response = await self.client.get('/api/me', headers={
                'Host':'evil.example', 'Origin':'https://bogdanova34.site'})
            self.assertEqual(response.status, 403)

    async def test_websocket_works_on_both_hosts_but_rejects_cross_domain_origin(self):
        config = {'PUBLIC_ORIGIN':'https://bogdanova34.site',
                  'ADDITIONAL_PUBLIC_ORIGINS':'https://bogdanova34.ru'}
        with patch.dict('os.environ', config):
            for host in ('bogdanova34.site', 'bogdanova34.ru'):
                ws = await self.client.ws_connect('/api/live', headers={'Host':host, 'Origin':'https://'+host})
                await ws.send_json({'csrf':self.me['csrf'], 'camera_ids':[1]})
                self.assertEqual(set((await ws.receive_json(timeout=3))['cameras']), {'1'})
                await ws.close()
            with self.assertRaises(WSServerHandshakeError) as error:
                await self.client.ws_connect('/api/live', headers={
                    'Host':'bogdanova34.ru', 'Origin':'https://bogdanova34.site'})
            self.assertEqual(error.exception.status, 403)


if __name__ == '__main__':
    unittest.main()
