import json
import queue
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from backend.cameras.connections import ConnectionGate
from backend.cameras.motion import MotionMonitor
from backend.cameras.session import SharedSession, SessionChanged
from backend.cameras.relay_worker import Channel, reconcile_channels
from backend.cameras.vesta import Transport, TransportClosed


class Output:
    def __init__(self):self.items=[];self.motion_failed=threading.Event();self.reconnected=threading.Event()
    def put(self,value,*args,**kwargs):
        self.items.append(value)
        if value.get('type')=='motion' and value.get('error'):self.motion_failed.set()
        if value.get('type')=='session' and value.get('ready') and value.get('generation')==2:self.reconnected.set()


class FakeControl:
    count=0
    def __init__(self,*args,**kwargs):
        self.messages=queue.Queue();self.sent=[];self.closed=False;self.foreign_alarm_messages=0;self.session=0;self.starts=threading.Event();self.start_count=0
    def login(self,*args,**kwargs):
        type(self).count+=1;self.session=type(self).count;return {'AliveInterval':21}
    def command(self,name):return {'Name':name,'SessionID':'0x%08x'%self.session}
    def send(self,mid,payload):
        self.sent.append((mid,payload))
        if mid==1500:self.messages.put(((0,1501,0),b'{"Ret":100}'))
        if mid==1020:self.messages.put(((0,1021,0),b'{"Ret":107}'))
        if mid==1410 and payload['OPMonitor']['Action']=='Start':self.start_count+=1
        if self.start_count>=4:self.starts.set()
    def receive(self,timeout):
        item=self.messages.get(timeout=timeout)
        if item==('closed',0):raise TransportClosed()
        return item
    def enqueue(self,item):self.messages.put(item)
    def close(self):self.closed=True


class FakeMedia:
    all=[]
    def __init__(self,*args,**kwargs):self.session=0;self.closed=False;self.claims=[];type(self).all.append(self)
    def login(self,*args,**kwargs):raise AssertionError('Media must reuse the authenticated session')
    def command(self,name):return {'Name':name,'SessionID':'0x%08x'%self.session}
    monitor_command=Transport.monitor_command
    def monitor(self,channel,action,mid,stream='Extra1'):self.claims.append((channel,action,mid,self.session))
    def receive(self,timeout):threading.Event().wait(.01);raise queue.Empty()
    def close(self):self.closed=True


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        FakeControl.count=0;FakeMedia.all=[];self.stop=threading.Event();self.out=Output();self.gate=ConnectionGate(2)
        self.owner=SharedSession(object(),{'SERIAL_NUM':'test'},self.stop,self.gate,self.out)
    def tearDown(self):self.stop.set();self.owner.close();self.gate.close()

    def test_sd_and_hd_share_login_but_stop_only_their_own_profile(self):
        with patch('backend.cameras.session.Transport',FakeControl),patch('backend.cameras.relay_worker.Transport',FakeMedia):
            self.owner.start();lease=self.owner.wait(self.stop)
            channels=[Channel(cid,'token',object(),{'SERIAL_NUM':'test'},self.out,self.gate,threading.Semaphore(),self.owner) for cid in (1,257)]
            try:
                # Wait on observed Start commands, never a wall-clock assertion.
                for channel in channels:
                    for _ in range(100):
                        if any(mid==1410 and value['OPMonitor']['Action']=='Start' and value['OPMonitor']['Parameter']['StreamType']==channel.profile for mid,value in lease.transport.sent):break
                        threading.Event().wait(.01)
                self.assertEqual(FakeControl.count,1)
                starts=[value['OPMonitor']['Parameter'] for mid,value in lease.transport.sent if mid==1410 and value['OPMonitor']['Action']=='Start']
                self.assertEqual({p['StreamType'] for p in starts},{'Extra1','Main'})
                self.assertEqual({p['Channel'] for p in starts},{0})
                channels[1].stop.set();channels[1].thread.join(timeout=5)
                self.assertTrue(channels[0].thread.is_alive())
                stops=[value['OPMonitor']['Parameter'] for mid,value in lease.transport.sent if mid==1410 and value['OPMonitor']['Action']=='Stop']
                self.assertEqual(stops,[dict(Channel=0,CombinMode='NONE',StreamType='Main',TransMode='TCP')])
            finally:
                for channel in channels:channel.stop.set()
                for channel in channels:channel.thread.join(timeout=5)

    def test_four_sources_share_one_login_and_control_start_stop(self):
        with patch('backend.cameras.session.Transport',FakeControl),patch('backend.cameras.relay_worker.Transport',FakeMedia):
            self.owner.start();lease=self.owner.wait(self.stop)
            channels=[Channel(cid,'token',object(),{'SERIAL_NUM':'test'},self.out,self.gate,threading.Semaphore(),self.owner) for cid in range(1,5)]
            try:
                self.assertTrue(lease.transport.starts.wait(timeout=5))
                self.assertEqual(FakeControl.count,1);self.assertEqual(len(FakeMedia.all),4)
                self.assertTrue(all(media.session==lease.transport.session for media in FakeMedia.all))
                self.assertTrue(all(media.claims[0][1:3]==('Claim',1413) for media in FakeMedia.all))
            finally:
                for channel in channels:channel.stop.set()
                for channel in channels:channel.thread.join(timeout=5);self.assertFalse(channel.thread.is_alive())
            stops=[data['OPMonitor']['Parameter']['Channel'] for mid,data in lease.transport.sent if mid==1410 and data['OPMonitor']['Action']=='Stop']
            self.assertEqual(set(stops),{0,1,2,3});self.assertTrue(all(media.closed for media in FakeMedia.all))

    def test_motion_dependency_failure_keeps_shared_session_and_does_not_log_in(self):
        with patch('backend.cameras.session.Transport',FakeControl):
            self.owner.start();lease=self.owner.wait(self.stop)
            monitor=MotionMonitor(object(),{'SERIAL_NUM':'test'},self.out,self.stop,None,session=self.owner)
            thread=threading.Thread(target=monitor.run,daemon=True);thread.start()
            try:
                self.assertTrue(self.out.motion_failed.wait(timeout=5))
                self.assertTrue(self.owner.is_current(lease));self.assertFalse(lease.transport.closed)
                self.assertEqual(FakeControl.count,1)
            finally:self.stop.set();thread.join(timeout=5);self.assertFalse(thread.is_alive())

    def test_acknowledged_keepalive_publishes_session_proof(self):
        verified=threading.Event();publish=self.out.put;proofs=[]
        def observe(value,*args,**kwargs):
            publish(value,*args,**kwargs)
            if value.get('type')=='session' and value.get('ready'):
                proofs.append(value)
                if len(proofs)==2:verified.set()
        self.out.put=observe
        with patch('backend.cameras.session.Transport',FakeControl):
            self.owner.start();lease=self.owner.wait(self.stop)
            lease.transport.enqueue(((0,1007,0),b'{"Ret":100}'))
            self.assertTrue(verified.wait(timeout=5))
            self.assertEqual([item['generation'] for item in proofs],[1,1])
            self.assertEqual(FakeControl.count,1)

    def test_old_lease_cannot_send_after_owner_closes(self):
        with patch('backend.cameras.session.Transport',FakeControl):
            self.owner.start();lease=self.owner.wait(self.stop);self.owner.close()
            self.assertFalse(self.owner.thread.is_alive());self.assertTrue(lease.transport.closed)
            with self.assertRaises(SessionChanged):self.owner.send(lease,1410,{})

    def test_shutdown_unblocks_sources_waiting_for_login(self):
        waiting=threading.Event();result=[]
        thread=threading.Thread(target=lambda:(waiting.set(),result.append(self.owner.wait(self.stop))),daemon=True);thread.start();waiting.wait(timeout=5)
        self.owner.close();thread.join(timeout=5);self.assertFalse(thread.is_alive());self.assertEqual(result,[None])

    def test_control_disconnect_replaces_lease_and_rejects_old_listener(self):
        with patch('backend.cameras.session.Transport',FakeControl):
            self.owner.start();old=self.owner.wait(self.stop);listener=self.owner.listen(self.stop)
            old.transport.enqueue(('closed',0))
            self.assertTrue(self.out.reconnected.wait(timeout=5))
            current=self.owner.wait(self.stop)
            self.assertEqual(current.generation,2);self.assertEqual(FakeControl.count,2)
            self.assertTrue(old.transport.closed)
            with self.assertRaises(SessionChanged):self.owner.send(old,1410,{})
            with self.assertRaises(SessionChanged):listener.receive(.1)
            self.owner.send(current,1410,{'OPMonitor':{'Action':'Start'}})
            metadata=[item for item in self.out.items if item.get('type')=='session']
            self.assertTrue(all(set(item)<= {'type','ready','logins','generation','error'} for item in metadata))

    def test_replacement_waits_for_old_stop_without_blocking_other_channels(self):
        alive=True
        old=SimpleNamespace(token='old',stop=threading.Event(),thread=SimpleNamespace(is_alive=lambda:alive))
        channels={1:old};created=[]
        def create(cid,token):
            created.append((cid,token))
            return SimpleNamespace(token=token,stop=threading.Event(),thread=SimpleNamespace(is_alive=lambda:True))
        reconcile_channels(channels,{1:'new',2:'other'},create)
        self.assertTrue(old.stop.is_set());self.assertIs(channels[1],old)
        self.assertEqual(created,[(2,'other')])
        alive=False
        reconcile_channels(channels,{1:'new',2:'other'},create)
        self.assertEqual(created,[(2,'other'),(1,'new')]);self.assertEqual(channels[1].token,'new')
        reconcile_channels(channels,{2:'other'},create)
        self.assertTrue(channels[1].stop.is_set());self.assertFalse(channels[2].stop.is_set())
