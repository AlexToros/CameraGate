import asyncio
from datetime import datetime
import json
import queue
import struct
import threading
import unittest
from unittest.mock import patch

from backend.cameras.motion import MotionState, MotionMonitor
from backend.cameras.recorder_worker import search, playback, frame_time, login
from backend.cameras.vesta import RequestTimeout, Frames
from backend.cameras.relay import RelayPool
from backend.archive import search_input
from backend.store import Problem
from tests.backend.test_protocol import keyframe


class MotionTests(unittest.TestCase):
    def test_human_event_survives_video_mask_refresh_but_expires_without_stop(self):
        state=MotionState()
        event={'AlarmInfo':{'Event':'appEventHumanDetectAlarm','Channel':'13','Status':'Start'}}
        with patch('backend.cameras.motion.time.monotonic',return_value=100):
            self.assertTrue(state.alarm(event))
            state.reconcile({'WorkState':{'AlarmState':{'VideoMotion':'0x1'}}})
            self.assertEqual(state.active,{1,14})
        with patch('backend.cameras.motion.time.monotonic',return_value=111):
            self.assertEqual(state.active,{1})
            state.alarm(event)
            event['AlarmInfo']['Status']='Stop';state.alarm(event)
            self.assertEqual(state.active,{1})

    def test_monitor_dependency_failure_does_not_stop_hub_or_live_workers(self):
        published=[]
        class Stop:
            value=False
            def is_set(self):return self.value
            def wait(self,timeout):self.value=True
        class Output:
            def put(self,meta,**kwargs):published.append(meta)
        stopping=Stop()
        with patch('backend.cameras.motion.Transport',side_effect=ConnectionError()):
            MotionMonitor(object(),{'SERIAL_NUM':'test'},Output(),stopping,threading.Semaphore()).run()
        self.assertEqual(published[-1]['available'],False)
        self.assertEqual(published[-1]['error'],'ConnectionError')
    def test_recorder_bitmask_events_and_channel_indexing(self):
        state=MotionState()
        state.reconcile({'WorkState':{'AlarmState':{'VideoMotion':'0x8000000000000005'}}})
        self.assertEqual(state.active,{1,3,64})
        state.alarm({'AlarmInfo':{'Event':'VideoMotion','Channel':0,'Status':'Stop'}})
        state.alarm({'Event':'VideoMotion','Channel':1,'Status':'Start'})
        state.alarm({'Event':'VideoLoss','Channel':3,'Status':'Start'})
        state.alarm({'Event':'VideoMotion','Channel':True,'Status':'Start'})
        self.assertEqual(state.active,{2,3,64})
        self.assertTrue(state.alarm({'AlarmInfo':{'Event':'VideoMotion','Channel':'4','Status':'Start'}}))
        self.assertIn(5,state.active)
        with self.assertRaises(ValueError):
            state.reconcile({'WorkState':{'AlarmState':{'VideoMotion':-1}}})

    def test_motion_logging_is_allowlisted_and_heartbeat_is_bounded(self):
        pool=RelayPool()
        meta=dict(type='motion',available=True,camera_ids=[1],method='events',diagnostics=dict(notifications=0,state_checks=1,password='private'))
        with patch('backend.cameras.relay.MOTION_LOG.info') as log:
            with patch('backend.cameras.relay.time.monotonic',return_value=100):pool.ingest(meta,b'')
            with patch('backend.cameras.relay.time.monotonic',return_value=105):pool.ingest(meta,b'')
            self.assertEqual(log.call_count,1)
            with patch('backend.cameras.relay.time.monotonic',return_value=161):pool.ingest(meta,b'')
            self.assertEqual(log.call_count,2)
        self.assertNotIn('password',pool.motion_details)

    def test_media_metrics_separate_callback_gap_from_ipc_delay(self):
        from tests.backend.test_relay import initialized
        source=initialized()
        source.last_frame=100
        with patch('backend.cameras.relay.time.monotonic',return_value=101),patch('backend.cameras.relay.time.time',return_value=1000):
            source.feed(dict(type='media',generation='first',key=False,worker_at=999.997,sdk_gap=1),b'frame')
        self.assertAlmostEqual(source.max_ipc_delay,.003)
        self.assertEqual(source.max_sdk_gap,1);self.assertEqual(source.arrival_gaps,1)
        self.assertEqual(source.received_frames,2)

    def test_one_subscription_and_one_bulk_state_query_for_many_channels(self):
        stopping=threading.Event();published=[];commands=[]
        messages=[(1501,{'Ret':100}),(1021,{'Ret':100,'WorkState':{'AlarmState':{'VideoMotion':'0xffff'}}}),
                  (1504,{'AlarmInfo':{'Event':'VideoMotion','Channel':20,'Status':'Start'}})]
        class Transport:
            def __init__(self,*args,**kwargs):pass
            def login(self,*args,**kwargs):return {}
            def send(self,mid,payload):commands.append(mid)
            def command(self,name):return {'Name':name}
            def receive(self,timeout):
                if not messages:
                    stopping.set();raise queue.Empty()
                mid,value=messages.pop(0)
                return (0,mid,0),json.dumps(value).encode()
            def close(self):pass
        class Output:
            def put(self,meta,**kwargs):published.append(meta)
        with patch('backend.cameras.motion.Transport',Transport),patch('backend.cameras.motion.time.monotonic',return_value=100):
            MotionMonitor(None,{'SERIAL_NUM':'test'},Output(),stopping,threading.Semaphore()).run()
        self.assertEqual(commands.count(1500),1)
        self.assertEqual(commands.count(1020),1)
        self.assertEqual(published[-1]['method'],'events')
        self.assertEqual(published[-1]['camera_ids'],[*range(1,17),21])

    def test_motion_expiry_acl_filter_and_config_replacement(self):
        pool=RelayPool()
        with patch('backend.cameras.relay.time.monotonic',return_value=100):
            pool.ingest(dict(type='motion',available=True,camera_ids=[1,2,3]),b'')
            self.assertEqual(pool.motion({2,4}),dict(available=True,camera_ids=[2]))
        with patch('backend.cameras.relay.time.monotonic',return_value=111):
            self.assertEqual(pool.motion({1,2}),dict(available=False,camera_ids=[]))
        asyncio.run(pool.replace(None))
        self.assertEqual(pool.motion({1})['camera_ids'],[])


class ArchiveProtocolTests(unittest.TestCase):
    def test_export_stops_at_selected_end_even_if_device_sends_a_whole_file(self):
        from types import SimpleNamespace
        commands=[];emitted=[]
        stamp=((2026-2000)<<26)|(10<<22)|(20<<17)
        class Parser:
            def feed_units(self,body,key_times):
                key_times[0]=stamp
                return [(b'frame',index==0,2,10) for index in range(100)]
        class Transport:
            def command(self,name):return {}
            def request(self,*args,**kwargs):return (),{'Ret':100}
            def send(self,mid,payload):commands.append(payload.copy())
            def receive(self,timeout):return (0,1422,0),b'whole file'
        muxer=SimpleNamespace(mime='video/mp4',init=b'init',codec=2,fps=10,parameters=(),fragment=lambda video,key:b'fragment')
        entry=dict(cid=1,file='/private/file',start='2026-10-20 00:00:00',end='2026-10-20 01:00:00')
        with patch('backend.cameras.recorder_worker.Frames',Parser),patch('backend.cameras.recorder_worker.Muxer',return_value=muxer),patch('backend.cameras.recorder_worker.parameter_sets',return_value=()):
            playback(Transport(),dict(entry=entry,offset=0,end_offset=2),lambda meta,data=b'':emitted.append((meta,data)))
        self.assertEqual(sum(meta['type']=='media' for meta,_ in emitted),20)
        self.assertEqual(commands[0]['OPPlayBack']['EndTime'],'2026-10-20 00:00:02')
        self.assertEqual(commands[-1]['OPPlayBack']['Action'],'Stop')
        self.assertEqual(emitted[-1][0]['type'],'end')

    def test_fragmented_batch_preserves_each_keyframe_time(self):
        one=bytearray(keyframe(b'first'));two=bytearray(keyframe(b'second'))
        struct.pack_into('<I',one,8,111);struct.pack_into('<I',two,8,222)
        parser=Frames();times={}
        self.assertEqual(parser.feed_units(one[:10],key_times=times),[])
        units=parser.feed_units(one[10:]+two,key_times=times)
        self.assertEqual(len(units),2);self.assertEqual(times,{0:111,1:222})
    def test_archive_login_replaces_timed_out_socket_once_without_reinitializing_sdk(self):
        calls=[]
        class Transport:
            def login(self,config,timeout):
                calls.append('login')
                if len(calls)==1:raise RequestTimeout(1000)
                return {'Ret':100}
            def reopen(self,serial):calls.append('reopen')
        self.assertEqual(login(Transport(),{'SERIAL_NUM':'test'}),{'Ret':100})
        self.assertEqual(calls,['login','reopen','login'])
    def test_archive_frame_time_uses_recorders_clock_and_invalid_header_is_unknown(self):
        stamp=((2026-2000)<<26)|(10<<22)|(20<<17)|(12<<12)|(34<<6)|56
        self.assertEqual(frame_time(stamp),datetime(2026,10,20,12,34,56))
        self.assertIsNone(frame_time(0));self.assertIsNone(frame_time(None))
    def test_search_validation_does_not_accept_other_channels_or_dates(self):
        self.assertEqual(search_input(dict(camera_id=1,date='2026-10-20'),{1}),(1,'2026-10-20',None))
        for value in [dict(camera_id=True,date='2026-10-20'),dict(camera_id=2,date='2026-10-20'),
                      dict(camera_id=1,date='2026-02-30'),dict(camera_id=1,date='2026-10-20',cursor='2026-10-21 00:00:00')]:
            with self.assertRaises(Problem):search_input(value,{1})

    def test_one_archive_query_per_page_and_nonprogressing_cursor_terminates(self):
        class Transport:
            calls=[]
            def command(self,name):return {'Name':name}
            def request(self,mid,payload,**kwargs):
                self.calls.append((mid,payload))
                return (),dict(Ret=111,OPFileQuery=[dict(FileName=f'/record/{i}',BeginTime='2026-10-20 00:00:00',EndTime='2026-10-20 01:00:00') for i in range(64)])
        transport=Transport()
        result=search(transport,dict(cid=2,date='2026-10-20'))
        self.assertEqual(len(transport.calls),1)
        self.assertEqual(transport.calls[0][1]['OPFileQuery']['Channel'],1)
        self.assertIsNone(result['cursor'])
        self.assertEqual(len(result['files']),64)

    def test_empty_archive_uses_successful_no_files_code(self):
        class Transport:
            def command(self,name):return {}
            def request(self,*args,**kwargs):return (),{'Ret':109}
        self.assertEqual(search(Transport(),dict(cid=1,date='2026-10-20')),dict(files=[],cursor=None))

    def test_playback_uses_exact_recording_and_requested_time_without_decoding(self):
        commands=[];emitted=[]
        class Transport:
            def command(self,name):return {'Name':name}
            def request(self,mid,payload,**kwargs):commands.append((mid,payload.copy()));return (),{'Ret':100}
            def send(self,mid,payload):commands.append((mid,payload.copy()))
            def receive(self,timeout):return (0,1423,0),b''
        entry=dict(cid=2,file='/record/channel2',start='2026-10-20 00:00:00',end='2026-10-20 01:00:00')
        with self.assertRaisesRegex(ValueError,'No archive frames'):
            playback(Transport(),dict(entry=entry,offset=120),lambda meta,data=b'':emitted.append(meta))
        request=commands[0][1]['OPPlayBack']
        self.assertEqual(request['Parameter']['PlayMode'],'ByName')
        self.assertEqual(request['Parameter']['Channel'],1)
        self.assertEqual(request['Parameter']['FileName'],'/record/channel2')
        self.assertEqual(request['StartTime'],'2026-10-20 00:02:00')
        self.assertEqual(commands[-1][1]['OPPlayBack']['Action'],'Stop')
        self.assertEqual(emitted,[])
