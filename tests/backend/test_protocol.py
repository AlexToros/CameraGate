import struct
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from backend.cameras.vesta import Frames, HEADER, Viewer, IsolatedViewer, Transport, frame_health, RequestTimeout
from backend.cameras.vesta import decoder_command
from backend.cameras.streams import worker_command


class RuntimeCommandTests(unittest.TestCase):
    def test_linux_worker_uses_current_interpreter_without_rosetta(self):
        with patch('backend.cameras.streams.sys.platform', 'linux'), patch('backend.cameras.streams.sys.executable', '/venv/bin/python'):
            command = worker_command('--worker', 4, 1)
        self.assertEqual(command[0], '/venv/bin/python')
        self.assertEqual(command[-3:], ['--worker', '4', '1'])
        self.assertNotIn('/usr/bin/arch', command)

    def test_configured_ffmpeg_decodes_both_codecs_with_one_thread(self):
        with patch.dict('os.environ', {'FFMPEG_BINARY': '/usr/bin/ffmpeg'}):
            hevc, avc = decoder_command(3, 12), decoder_command(2, 25)
        self.assertEqual(hevc[0], '/usr/bin/ffmpeg')
        self.assertIn('hevc', hevc)
        self.assertIn('h264', avc)
        self.assertEqual(hevc[hevc.index('-threads')+1], '1')


def keyframe(data, codec=3):
    return b'\0\0\1\xfc' + bytes([codec, 12, 100, 56]) + bytes(4) + struct.pack('<I', len(data)) + data


def delta(data):
    return b'\0\0\1\xfd' + struct.pack('<I', len(data)) + data


class MediaTests(unittest.TestCase):
    def test_fragmented_frames_match_whole_input_and_skip_audio(self):
        key = b'\0\0\0\1\x40\x01{random-compressed-data}'
        change = b'\0\0\0\1\x02\x01delta'
        audio = b'\0\0\1\xfa\x0e\x02\x04\x00abcd'
        data = keyframe(key) + audio + delta(change)
        parser = Frames()
        result = b''.join(parser.feed(data[i:i + 7]) for i in range(0, len(data), 7))
        self.assertEqual(result, key + change)
        self.assertEqual(parser.codec, 3)
        self.assertEqual(parser.buffer, b'')

    def test_skip_initial_incomplete_gop(self):
        parser = Frames()
        self.assertEqual(parser.feed(b'random tail' + delta(b'incomplete')), b'')
        self.assertEqual(parser.feed(keyframe(b'key') + delta(b'next')), b'keynext')

    def test_frame_limit(self):
        with self.assertRaises(ValueError):
            Frames().feed(b'\0\0\1\xfd' + struct.pack('<I', 9 * 1024 * 1024))

    def test_dvrip_header_layout(self):
        self.assertEqual(HEADER.size, 20)
        header = HEADER.pack(255, 0, 7, 3, 0, 0, 1412, 1234)
        self.assertEqual(struct.unpack_from('<H', header, 14)[0], 1412)
        self.assertEqual(struct.unpack_from('<I', header, 16)[0], 1234)

    def test_camera_frame_rate_is_extracted_from_keyframe(self):
        parser = Frames()
        parser.feed(keyframe(b'video'))
        self.assertEqual(parser.fps, 12)


class RecoveryTests(unittest.TestCase):
    def test_transport_discards_packet_from_previous_dvrip_session(self):
        transport = Transport.__new__(Transport)
        transport.session = 2
        transport.buffer = bytearray(HEADER.pack(255, 0, 1, 0, 0, 0, 1412, 3) + b'old' +
                                     HEADER.pack(255, 0, 2, 0, 0, 0, 1412, 3) + b'new')
        transport.overflow = threading.Event()
        transport.events = queue.Queue()
        fields, data = transport.receive()
        self.assertEqual(fields[2], 2)
        self.assertEqual(data, b'new')

    def test_late_frame_from_previous_visit_to_same_camera_is_rejected(self):
        viewer = IsolatedViewer()
        viewer.channels = ['one', 'two']
        viewer.select(1)
        viewer.select(0)
        old = {'selected': 0, 'generation': 0, 'frame_generation': 0,
               'channels': ['one', 'two'], 'state': 'Прямой эфир', 'live': True}
        self.assertFalse(viewer.accept_packet(old, b'old camera jpeg'))
        self.assertIsNone(viewer.jpeg)
        current = {**old, 'generation': 2, 'frame_generation': 2}
        self.assertTrue(viewer.accept_packet(current, b'fresh camera jpeg'))
        self.assertTrue(viewer.snapshot()['live'])

    def test_matching_selection_does_not_accept_old_decoder_frame(self):
        viewer = IsolatedViewer()
        meta = {'selected': 0, 'generation': 0, 'frame_generation': -1,
                'channels': [], 'state': 'Прямой эфир', 'live': True}
        self.assertFalse(viewer.accept_packet(meta, b'old decoder jpeg'))
        self.assertIsNone(viewer.jpeg)

    def test_login_timeout_retries_on_new_device_socket(self):
        viewer = Viewer()
        transport = Mock()
        transport.login.side_effect = [RequestTimeout(1000), {'ChannelNum': 2}]
        result = viewer.reconnect(transport, {'SERIAL_NUM': 'placeholder'})
        self.assertEqual(result, {'ChannelNum': 2})
        self.assertEqual(transport.reopen.call_count, 2)
        self.assertIn('device_retry', viewer.timings)

    def test_device_retry_is_bounded(self):
        viewer = Viewer()
        transport = Mock()
        transport.login.side_effect = RequestTimeout(1000)
        with self.assertRaises(RequestTimeout):
            viewer.reconnect(transport, {'SERIAL_NUM': 'placeholder'})
        self.assertEqual(transport.reopen.call_count, 2)

    def test_media_without_decoded_frames_is_not_healthy(self):
        self.assertEqual(frame_health(100, 80, 100, 0, False), 'decoded_frames_timeout')
        self.assertEqual(frame_health(100, 80, 100, 90, False), 'decoded_frames_timeout')
        self.assertIsNone(frame_health(100, 80, 100, 99, False))
        self.assertEqual(frame_health(100, 80, 100, 99, True), 'decoder_exited')

    def test_audio_only_stream_triggers_recovery_even_with_media_packets(self):
        clock = [100.0]
        audio = b'\0\0\1\xfa\x0e\x02\x04\x00abcd'
        transport = Mock()
        transport.login.return_value = {'ChannelNum': 2, 'AliveInterval': 21}
        transport.request.return_value = (None, {'ChannelTitle': ['one', 'two']})

        def receive(_):
            clock[0] += 1
            return (255, 0, 1, 0, 0, 0, 1412, len(audio)), audio

        transport.receive.side_effect = receive
        with patch('backend.cameras.vesta.credentials', return_value={'SERIAL_NUM': 'placeholder'}), \
             patch('backend.cameras.vesta.discover', return_value='127.0.0.1'), \
             patch('backend.cameras.vesta.Transport', return_value=transport), \
             patch('backend.cameras.vesta.time.monotonic', side_effect=lambda: clock[0]):
            viewer = Viewer()
            viewer.run()
        self.assertEqual(viewer.failure, 'decoded_frames_timeout')
        self.assertGreater(viewer.media_packets, 0)
        self.assertEqual(viewer.channels, ['one', 'two'])
        transport.request.assert_called_once()
        transport.close.assert_called_once()
        self.assertIsNone(viewer.decoder)

    def test_fixed_media_worker_does_not_repeat_catalog_query(self):
        transport = Mock()
        transport.login.return_value = {'ChannelNum': 36, 'AliveInterval': 21}
        viewer = Viewer({'SERIAL_NUM':'placeholder'})
        viewer.catalog_required = False
        with patch('backend.cameras.vesta.discover',return_value='127.0.0.1'), \
             patch('backend.cameras.vesta.Transport',return_value=transport), \
             patch('backend.cameras.vesta.frame_health',return_value='media_timeout'):
            viewer.run()
        transport.request.assert_not_called()
        transport.login.assert_called_once()
        self.assertEqual(len(viewer.channels),36)
        transport.close.assert_called_once()

    def test_hung_worker_is_killed_if_it_ignores_termination(self):
        viewer = IsolatedViewer()
        viewer.child = Mock()
        viewer.child.poll.return_value = None
        viewer.stop = Mock()
        viewer.stop.wait.side_effect = [False, False, True]
        with patch('backend.cameras.vesta.time.monotonic', side_effect=[10, 13]):
            viewer.supervise()
        viewer.child.terminate.assert_called_once()
        viewer.child.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
