import struct
import unittest
from unittest.mock import patch

from backend.cameras.relay_worker import Outbox
from backend.cameras.audio import AudioClock, HEADER, MAX_AUDIO
from backend.cameras.relay import EncodedSource, pack_audio
from backend.cameras.vesta import Frames
from tests.backend.test_protocol import keyframe
from tests.backend.test_relay import initialized, records


class AudioTests(unittest.TestCase):
    def test_audio_backpressure_drops_audio_without_blocking_video_worker(self):
        box=Outbox(limit=100)
        box.put({'type':'media'},b'video')
        before=box.size
        self.assertFalse(box.put({'type':'audio'},b'x'*100,timeout=0))
        self.assertEqual(box.size,before)
        self.assertTrue(box.get().endswith(b'video'))

    def test_fragmented_audio_and_video_preserve_legacy_video_only_output(self):
        audio=b'\0\0\1\xfa\x0e\x02'+struct.pack('<H',320)+b'\xd5'*320
        mixed=audio+keyframe(b'video')+audio
        parser=Frames();chunks=[]
        self.assertEqual(parser.feed_units(mixed[:80],chunks),[])
        self.assertEqual(parser.feed_units(mixed[80:],chunks),[(b'video',True,3,12)])
        self.assertEqual(chunks,[b'\xd5'*320,b'\xd5'*320])
        self.assertEqual(Frames().feed(mixed),b'video')

    def test_clock_batches_multiple_frames_and_bounds_backlog(self):
        clock=AudioClock()
        data=clock.batch([b'a'*320,b'b'*320],1)
        self.assertEqual(HEADER.unpack_from(data),(14,2,.92))
        self.assertEqual(data[10:],b'a'*320+b'b'*320)
        self.assertAlmostEqual(HEADER.unpack_from(clock.batch([b'c'*320],1.04))[2],1)
        latest=clock.batch([b'd'*16000],20)
        self.assertEqual(len(latest),8010)
        self.assertEqual(HEADER.unpack_from(latest)[2],19)

    def test_independent_cursors_only_selected_sources_latest_start_and_generation(self):
        one,two=initialized(1),initialized(2)
        payload=HEADER.pack(14,2,0)+b'first'
        one.feed(dict(type='audio',generation='first'),payload)
        two.feed(dict(type='audio',generation='first'),payload)
        a,b={},{}
        self.assertEqual(records(pack_audio({1:one},a)),[(1,4,payload)])
        self.assertEqual(records(pack_audio({2:two},b)),[(2,4,payload)])
        self.assertFalse(pack_audio({1:one},a))
        one.feed(dict(type='audio',generation='first'),HEADER.pack(14,2,.04)+b'next')
        self.assertEqual(len(records(pack_audio({1:one},a))),1)
        latest=records(pack_audio({1:one},{}));self.assertEqual(len(latest),1)
        self.assertTrue(latest[0][2].endswith(b'next'))
        one.feed(dict(type='init',generation='new',mime='video/mp4'),b'new-init')
        one.feed(dict(type='audio',generation='first'),payload)
        self.assertFalse(one.audio)
        self.assertFalse(pack_audio({1:one},a))

    def test_audio_buffer_and_expiry_are_bounded_without_affecting_video(self):
        source=initialized()
        data=HEADER.pack(14,2,0)+b'x'*8000
        with patch('backend.cameras.relay.time.monotonic',return_value=100):
            source.last_frame=100
            source.feed(dict(type='audio',generation='first'),data)
            source.feed(dict(type='audio',generation='first'),data)
            source.feed(dict(type='audio',generation='first'),data)
            self.assertLessEqual(source.audio_size,MAX_AUDIO)
            self.assertEqual(len(source.audio),1)
            self.assertTrue(pack_audio({1:source},{}))
        with patch('backend.cameras.relay.time.monotonic',return_value=102):
            self.assertFalse(pack_audio({1:source},{}))
        self.assertTrue(source.frames)
        source.reset();self.assertFalse(source.audio);self.assertEqual(source.audio_size,0)
