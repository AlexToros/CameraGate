import json
from pathlib import Path
import queue
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.cameras.event_log import EventJournal, MAX_BODY
from backend.cameras.relay import RelayPool
from backend.cameras.vesta import Transport, HEADER


class EventJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'events.jsonl'
        self.config=dict(SERIAL_NUM='private-device',USERNAME='private-user',PASSWORD='private-password')
        self.journal=EventJournal(self.path,self.config)

    def tearDown(self):
        self.journal.close();self.tmp.cleanup()

    def wait_written(self,count):
        with self.journal.condition:
            self.assertTrue(self.journal.condition.wait_for(lambda:self.journal.written>=count,timeout=5))

    def test_unknown_events_keep_coordinates_and_nested_fields_without_credentials(self):
        value=dict(SessionID='0x12345678',AlarmInfo=dict(Event='UnknownFutureEvent',Channel='13',
            Rect=[10,20,30,40],Targets=[dict(x=.2,y=.4)],Metadata=dict(PasswordHash='other-hash',
            Access_Token='other-token',Text='private-device private-user private-password'),
            Extra=json.dumps(dict(SessionID='nested-session',Rect=[1,2,3,4]))))
        self.journal.record((0,1504,0),json.dumps(value).encode(),14)
        self.wait_written(1)
        text=self.path.read_text();entry=json.loads(text)
        self.assertEqual(entry['body']['AlarmInfo']['Rect'],[10,20,30,40])
        self.assertEqual(entry['body']['AlarmInfo']['Targets'],[dict(x=.2,y=.4)])
        self.assertEqual(entry['stream_channel'],14)
        for secret in ('0x12345678','other-hash','other-token','private-device','private-user','private-password','nested-session'):
            self.assertNotIn(secret,text)

    def test_transport_captures_all_control_types_but_not_auth_media_or_foreign_session(self):
        transport=Transport.__new__(Transport)
        transport.hub=None;transport.event_journal=self.journal;transport.session=7
        transport.overflow=threading.Event();transport.events=queue.Queue()
        # Complete in-memory packets; no SDK/network or recorder logins.
        packets=[(7,1001,b'{"Password":"hidden"}'),(7,1412,b'private-video'),
            (6,1504,b'{"Event":"foreign"}'),(7,1599,b'{"Unknown":{"Coordinates":[1,2]}}'),
            (7,1021,b'{"Ret":100}'),(7,1007,b'{"Ret":100}')]
        transport.buffer=bytearray(b''.join(HEADER.pack(255,0,sid,0,0,0,mid,len(body))+body for sid,mid,body in packets))
        transport.foreign_alarm_messages=0
        received=[transport.receive()[0][-2] for _ in range(5)]
        self.assertEqual(received,[1001,1412,1599,1021,1007]);self.wait_written(3)
        entries=[json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([e['message_id'] for e in entries],[1599,1021,1007])
        self.assertNotIn('private-video',self.path.read_text());self.assertNotIn('foreign',self.path.read_text())

    def test_slow_disk_does_not_block_receiver_and_queue_is_bounded(self):
        entered=threading.Event();release=threading.Event()
        def slow(record):entered.set();release.wait(timeout=5)
        with patch.object(self.journal.handler,'emit',side_effect=slow):
            self.journal.record((0,1504,0),b'{}');self.assertTrue(entered.wait(timeout=5))
            self.journal.limit=1024
            for _ in range(20):self.journal.record((0,1504,0),b'{}')
            self.assertLessEqual(self.journal.stats()['queued_bytes'],1024)
            self.assertGreater(self.journal.stats()['dropped'],0)
            before=self.journal.stats()
            self.journal.record((0,1412,0),b'video')
            self.assertEqual(self.journal.stats(),before)
            release.set();self.wait_written(1)

    def test_write_failure_is_counted_and_does_not_kill_journal(self):
        with patch.object(self.journal.handler,'emit',side_effect=OSError('disk unavailable')):
            self.journal.record((0,1504,0),b'{}')
            with self.journal.condition:
                self.assertTrue(self.journal.condition.wait_for(lambda:self.journal.write_failures>0,timeout=5))
        self.assertTrue(self.journal.thread.is_alive())
        self.journal.record((0,1504,0),b'{"Ret":100}');self.wait_written(1)
        entries=[json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertTrue(any(e.get('type')=='journal_status' and e['dropped']==1 for e in entries))

    def test_invalid_and_large_bodies_are_explicitly_marked_without_leaking_raw_bytes(self):
        self.journal.record((0,1599,0),b'not json private-password')
        self.journal.record((0,1599,0),b'x'*(MAX_BODY+1))
        self.wait_written(2)
        entries=[json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([e['body_omitted'] for e in entries],['non_json','size_limit'])
        self.assertNotIn('private-password',self.path.read_text())

    def test_rotation_and_private_permissions(self):
        self.journal.handler.maxBytes=1024;self.journal.handler.backupCount=2
        for index in range(12):
            self.journal.record((0,1504,0),json.dumps(dict(Index=index,Data='z'*300)).encode())
            self.wait_written(index+1)
        self.journal.close()
        files=list(Path(self.tmp.name).glob('events.jsonl*'))
        self.assertEqual(len(files),3)
        self.assertTrue(all(stat.S_IMODE(file.stat().st_mode)==0o600 for file in files))

    def test_api_diagnostics_only_expose_log_counters(self):
        pool=RelayPool()
        pool.ingest(dict(type='heartbeat',event_log=dict(written=3,dropped=0,write_failures=1,
            queued_bytes=20,body='private-body',path='private-path')),b'')
        self.assertEqual(pool.diagnostics()['event_log'],dict(written=3,dropped=0,write_failures=1,queued_bytes=20))
        self.assertNotIn('private',json.dumps(pool.diagnostics()))
