import asyncio
import struct
import sys
import unittest
from unittest.mock import patch

from backend.cameras.mp4 import Muxer, box, codec_string, boxes, ReorderedStream, initialization
from backend.cameras.relay import EncodedSource, RelayPool, pack_encoded, MAX_GOP
from backend.cameras.vesta import Frames
from tests.backend.test_protocol import keyframe, delta


def records(data):
    output = []
    while data:
        cid, kind, size = struct.unpack_from('!IBI', data)
        output.append((cid,kind,data[9:9+size]))
        data = data[9+size:]
    return output


def initialized(cid=1, generation='first'):
    source = EncodedSource(cid)
    source.feed(dict(type='init',generation=generation,mime='video/mp4; codecs="hvc1.1.6.L90.B0"'),b'init')
    source.feed(dict(type='media',generation=generation,key=True),b'key')
    return source


class FragmentsTests(unittest.TestCase):
    def test_access_units_retain_key_boundaries_and_cadence(self):
        parser = Frames()
        self.assertEqual(parser.feed_units(delta(b'incomplete')+keyframe(b'key')+delta(b'delta')),
                         [(b'key',True,3,12),(b'delta',False,3,12)])

    def test_codec_string_and_fragment_payload_round_trip(self):
        hvcc = bytes([1,1])+bytes.fromhex('60000000b000000000005a')+bytes(10)
        init = box(b'hvcC',hvcc)
        self.assertEqual(codec_string(init,3),'hvc1.1.6.L90.B0')
        vps=b'\0\0\0\1\x40\x01parameter'
        picture=b'\0\0\1\x26\x01picture'
        with patch('backend.cameras.mp4.initialization',return_value=init):
            mux = Muxer(vps+picture,3,12)
        fragment = mux.fragment(vps+picture,True)
        values = dict(boxes(fragment))
        self.assertEqual(values[b'mdat'][8:],struct.pack('!I',9)+b'\x26\x01picture')
        self.assertEqual(struct.unpack_from('!I',fragment,96)[0],0x02000000)
        mux.fragment(b'\0\0\1\x02\x01next',False)
        self.assertEqual(mux.timestamp,15000)
        self.assertEqual(mux.sequence,2)

    def test_reordered_stream_is_rejected_without_transcoding(self):
        fake = type('Result',(),dict(stdout=box(b'ftyp',b'isom')+box(b'moov',b'metadata'),
            stderr=b'[trace_headers] 42 sps_max_num_reorder_pics[0] 010 = 1\n'))()
        with patch('backend.cameras.mp4.subprocess.run',return_value=fake) as run:
            with self.assertRaises(ReorderedStream):
                initialization(b'video',3,12)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-c:v')+1],'copy')
        self.assertNotIn('-vf',command)

    def test_independent_viewer_cursors_receive_cached_gop_then_deltas(self):
        source = initialized()
        source.feed(dict(type='media',generation='first',key=False),b'delta')
        first, second = {}, {}
        a = pack_encoded({1:source},first)[1]
        b = pack_encoded({1:source},second)[1]
        self.assertEqual(a,b)
        self.assertEqual([kind for cid,kind,data in records(a)],[1,3,2])
        self.assertFalse(pack_encoded({1:source},first)[1])
        source.feed(dict(type='media',generation='first',key=False),b'next')
        self.assertEqual(records(pack_encoded({1:source},first)[1]),[(1,2,b'next')])
        self.assertEqual(records(pack_encoded({1:source},second)[1]),[(1,2,b'next')])

    def test_gop_boundary_retains_unsent_tail_without_reinitializing_player(self):
        source = initialized();current={};slow={}
        pack_encoded({1:source},current);pack_encoded({1:source},slow)
        source.feed(dict(type='media',generation='first',key=False),b'missed')
        pack_encoded({1:source},current)
        source.feed(dict(type='media',generation='first',key=True),b'new-key')
        self.assertEqual([r[1] for r in records(pack_encoded({1:source},current)[1])],[3])
        self.assertEqual(records(pack_encoded({1:source},slow)[1]),[(1,2,b'missed'),(1,3,b'new-key')])
        newest={}
        self.assertEqual([r[1] for r in records(pack_encoded({1:source},newest)[1])],[1,3])
        source.feed(dict(type='media',generation='first',key=False),b'another')
        source.feed(dict(type='media',generation='first',key=True),b'third-key')
        source.feed(dict(type='media',generation='first',key=True),b'fourth-key')
        self.assertEqual(records(pack_encoded({1:source},slow)[1]),[(1,3,b'fourth-key')])

    def test_generation_restart_stale_packets_and_buffer_limit(self):
        source=initialized();sent={};pack_encoded({1:source},sent)
        source.feed(dict(type='init',generation='new',mime='video/mp4'),b'new-init')
        source.feed(dict(type='media',generation='first',key=True),b'stale')
        self.assertFalse(source.frames)
        source.feed(dict(type='media',generation='new',key=True),b'new-key')
        self.assertEqual([r[1] for r in records(pack_encoded({1:source},sent)[1])],[1,3])
        source.feed(dict(type='media',generation='new',key=False),b'x'*MAX_GOP)
        self.assertFalse(source.frames)
        source.feed(dict(type='media',generation='new',key=False),b'delta-after-overflow')
        self.assertFalse(source.frames)
        source.feed(dict(type='media',generation='new',key=True),b'recovered')
        self.assertEqual(source.size,len(b'recovered'))


class ContinuousPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_hd_is_shared_on_demand_and_expires_without_stopping_persistent_sd(self):
        pool=RelayPool({'test':True})
        try:
            with patch.object(pool,'run',side_effect=asyncio.Event().wait):
                with patch('backend.cameras.relay.time.monotonic',return_value=100):
                    pool.maintain([1,2])
                    one=pool.subscribe([1,257]);two=pool.subscribe([1,257])
                    self.assertIs(one[257],two[257]);self.assertEqual(set(pool.sources),{1,2,257})
                with patch('backend.cameras.relay.time.monotonic',return_value=102):
                    pool.subscribe([257]);pool.prune();self.assertIn(257,pool.sources)
                with patch('backend.cameras.relay.time.monotonic',return_value=104):
                    pool.prune();self.assertIn(257,pool.sources)
                with patch('backend.cameras.relay.time.monotonic',return_value=105):
                    pool.prune();self.assertEqual(set(pool.sources),{1,2})
        finally:await pool.close()

    async def test_visible_demands_merge_without_promoting_background_sources(self):
        pool=RelayPool({'test':True})
        try:
            with patch.object(pool,'run',side_effect=asyncio.Event().wait),patch('backend.cameras.relay.time.monotonic',return_value=100):
                pool.maintain([1,2,3])
                pool.subscribe([1]);pool.subscribe([2,3],priority_ids=[2])
                self.assertEqual({cid for cid,s in pool.sources.items() if s.demanded>99},{1,2})
                self.assertEqual(set(pool.sources),{1,2,3})
                self.assertEqual(pool.sources[3].touched,100)
        finally:await pool.close()

    async def test_one_worker_serves_multiple_sources_and_recovers_after_process_exit(self):
        # Exercise real process/pipe lifecycle without the proprietary SDK or
        # recorder. Two source records cross one IPC pipe in a bounded batch.
        worker = '''
import json,os,struct,sys,time
initial=json.loads(sys.stdin.readline())
generation=sys.argv[1]
packets=[]
for cid in initial['camera_ids']:
    base=dict(cid=cid,token=initial['tokens'][str(cid)],generation=generation)
    for meta,data in [(dict(base,type='init',mime='video/mp4'),b'init'),(dict(base,type='media',key=True),b'key')]:
        header=json.dumps(meta).encode()
        packets.append(struct.pack('<II',len(header),len(data))+header+data)
sys.stdout.buffer.write(b''.join(packets));sys.stdout.buffer.flush()
if generation=='1':
    time.sleep(.3)
    os._exit(0)
sys.stdin.read()
'''
        create = asyncio.create_subprocess_exec
        processes=[]
        async def launch(*args,**kwargs):
            process=await create(sys.executable,'-c',worker,str(len(processes)+1),**kwargs)
            processes.append(process)
            return process
        async def ready(pool,generation):
            for _ in range(200):
                if pool.sources and all(s.frames and s.generation==generation for s in pool.sources.values()):
                    return
                await asyncio.sleep(.02)
            self.fail('Worker did not produce the expected generation')
        pool=RelayPool({'test':True})
        try:
            with patch('backend.cameras.relay.asyncio.create_subprocess_exec',side_effect=launch):
                pool.maintain([1,2])
                await ready(pool,'1')
                pool.subscribe([1,2]);pool.subscribe([2,1])
                self.assertEqual(len(processes),1)
                await ready(pool,'2')
                self.assertEqual(len(processes),2)
                self.assertEqual(set(pool.sources),{1,2})
                await pool.replace(None)
                self.assertTrue(all(p.returncode is not None for p in processes))
                self.assertFalse(pool.sources)
        finally:
            await pool.close()

    async def test_old_worker_token_and_global_cache_bound(self):
        pool=RelayPool({'test':True})
        with patch.object(pool,'run',side_effect=asyncio.Event().wait):
            pool.maintain([1,2])
            one,two=pool.sources[1],pool.sources[2]
            pool.ingest(dict(cid=1,token='old',type='init',generation='old',mime='video/mp4'),b'stale')
            self.assertFalse(one.init)
            with patch('backend.cameras.relay.MAX_CACHE',20):
                for source in (one,two):
                    base=dict(cid=source.cid,token=source.token,generation='new')
                    pool.ingest(dict(base,type='init',mime='video/mp4'),b'init')
                    pool.ingest(dict(base,type='media',key=True),b'12345678')
                self.assertLessEqual(pool.cache_bytes,20)
                self.assertTrue(one.frames)
                self.assertFalse(two.frames)
                self.assertTrue(two.init)
            pool.maintain([]);pool.prune()
            self.assertEqual(pool.cache_bytes,0)
            await pool.close()

    async def test_group_removal_retains_only_actual_viewer_demands(self):
        pool=RelayPool({'test':True})
        with patch.object(pool,'run',side_effect=asyncio.Event().wait):
            with patch('backend.cameras.relay.time.monotonic',return_value=100):
                pool.maintain([1,2,3])
                pool.subscribe([2])
                pool.maintain([3,4])
                pool.prune()
                self.assertEqual(set(pool.sources),{2,3,4})
            with patch('backend.cameras.relay.time.monotonic',return_value=116):
                pool.prune()
                self.assertEqual(set(pool.sources),{3,4})
            await pool.close()

    async def test_persistent_sources_start_without_viewers_share_one_worker_and_survive_logout(self):
        running = asyncio.Event()
        async def run():
            await running.wait()
        pool=RelayPool({'test':True})
        try:
            with patch.object(pool,'run',side_effect=run) as worker:
                pool.maintain([1,2,3])
                await asyncio.sleep(0)
                first=pool.subscribe([1,2,1]);second=pool.subscribe([2,3])
                self.assertIs(first[2],second[2])
                self.assertEqual(set(pool.sources),{1,2,3})
                worker.assert_awaited_once()
                self.assertEqual(pool.persistent,{1,2,3})
                pool.maintain([1,2,3])
                await asyncio.sleep(0)
                worker.assert_awaited_once()
                await pool.replace(None)
                self.assertFalse(pool.sources)
                self.assertIsNone(pool.task)
                self.assertEqual(pool.persistent,{1,2,3})
        finally:
            await pool.close()
