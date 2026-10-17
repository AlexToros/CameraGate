"""Local VestaCloud viewer. Credentials and decoded frames stay on this Mac."""
import ctypes as C
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
HEADER = struct.Struct('<BB2xIIBBHI')
OUT = sys.stdout
PORT = int(os.environ.get('VIEWER_PORT', '8080'))
LOG = logging.getLogger('viewer')
SERVER_ID = uuid.uuid4().hex


class RequestTimeout(TimeoutError):
    def __init__(self, mid):
        self.mid = mid
        super().__init__('Нет ответа регистратора на команду %d' % mid)


class TransportClosed(ConnectionError):
    pass


def diagnostic(event, **fields):
    # Only explicit timing/counter fields, never SDK payloads or device credentials.
    LOG.info(json.dumps({'event': event, **fields}, ensure_ascii=False))


def decoder_command(codec, fps):
    binary = next((ROOT / '.runtime/packages/imageio_ffmpeg/binaries').glob('ffmpeg-*'))
    return [str(binary), '-hide_banner', '-loglevel', 'error',
            '-probesize', '32', '-analyzeduration', '1', '-fpsprobesize', '0',
            '-threads', '1', '-flags', 'low_delay', '-r', str(fps),
            '-f', 'hevc' if codec == 3 else 'h264', '-i', 'pipe:0',
            '-an', '-vf', "fps=8,scale=w='min(1280,iw)':h=-2",
            '-q:v', '5', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']


def frame_health(now, started, last_media, last_jpeg, decoder_failed):
    if decoder_failed:
        return 'decoder_exited'
    if now - last_media > 12:
        return 'media_timeout'
    if now - (last_jpeg or started) > (8 if last_jpeg else 12):
        return 'decoded_frames_timeout'
    return None


def credentials():
    result = {}
    for line in (ROOT / '.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    if not all(result.get(k) for k in ('SERIAL_NUM', 'USERNAME', 'PASSWORD')):
        raise ValueError('В .env нужны SERIAL_NUM, USERNAME и PASSWORD')
    return result


def discover():
    request = (struct.pack('<I', 0x05f42014) + b'123456787890ab\0').ljust(104, b'\0')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(request, ('112.124.0.188', 7999))
        data, _ = sock.recvfrom(512)
    if len(data) < 24 or struct.unpack_from('<I', data)[0] != 0x05f52014:
        raise ConnectionError('Не получен адрес сервера P2P')
    address = data[4:24].split(b'\0', 1)[0].decode('ascii')
    socket.inet_aton(address)
    return address


class Transport:
    def __init__(self, address, serial, mark=lambda _: None):
        self.events = queue.Queue(maxsize=4096)
        self.overflow = threading.Event()
        self.buffer = bytearray()
        self.session = 0
        self.sequence = 0
        self.connected = threading.Condition()
        self.connected_ids = set()
        connect_cb = C.CFUNCTYPE(None, C.c_int)
        disconnect_cb = C.CFUNCTYPE(None, C.c_int, C.c_int)
        receive_cb = C.CFUNCTYPE(None, C.c_int, C.c_void_p, C.c_int)

        def put(item):
            try:
                self.events.put_nowait(item)
            except queue.Full:
                self.overflow.set()

        @connect_cb
        def connected(cid):
            with self.connected:
                self.connected_ids.add(cid)
                self.connected.notify_all()

        @disconnect_cb
        def disconnected(cid, reason):
            if cid == self.cid:
                put(('closed', reason))

        @receive_cb
        def received(cid, pointer, length):
            if cid == self.cid and 0 < length <= 1024 * 1024:
                put(C.string_at(pointer, length))

        class Callbacks(C.Structure):
            _fields_ = [('connect', connect_cb), ('disconnect', disconnect_cb),
                        ('receive', receive_cb), ('reserved', C.c_void_p * 3)]

        self.callbacks = Callbacks(connected, disconnected, received)
        self.sdk = C.CDLL(str(ROOT / '.runtime/libnetsdk.dylib'))
        self._bind('vv_nat_init', [C.c_char_p, C.c_ushort, C.c_char_p, C.c_ushort,
                                  C.POINTER(Callbacks), C.c_int])
        self._bind('vv_nat_get_status', [])
        self._bind('vv_nat_connect', [C.c_char_p, C.c_int])
        self._bind('vv_nat_send', [C.c_int, C.c_char_p, C.c_int])
        self._bind('vv_nat_close', [C.c_int])
        self._bind('vv_nat_exit', [])
        self._bind('vv_enable_connect_cache', [C.c_int])
        # The SDK otherwise wakes closed, already-claimed DVRIP connections.
        # A fresh DVRIP login needs a fresh device socket, not that idle cache.
        self.sdk.vv_enable_connect_cache(0)
        self.cid = 0
        if self.sdk.vv_nat_init(address.encode(), 8765, None, 0, C.byref(self.callbacks), 2) != 1:
            raise ConnectionError('Не удалось запустить P2P')
        deadline = time.monotonic() + 12
        while self.sdk.vv_nat_get_status() != 1:
            if time.monotonic() >= deadline:
                self.close()
                raise TimeoutError('Сервер P2P не отвечает')
            time.sleep(.1)
        mark('p2p_ready')
        self.connect_device(serial)
        mark('p2p_connected')

    def connect_device(self, serial):
        with self.connected:
            self.connected_ids.clear()
        self.cid = self.sdk.vv_nat_connect(serial.encode(), 1)
        if self.cid <= 0:
            raise TransportClosed('Регистратор недоступен через P2P')
        with self.connected:
            if not self.connected.wait_for(lambda: self.cid in self.connected_ids, timeout=3):
                raise TransportClosed('P2P не подтвердил готовность соединения')

    def _bind(self, name, args):
        fn = getattr(self.sdk, name)
        fn.argtypes = args
        fn.restype = C.c_int

    def send(self, mid, payload):
        body = json.dumps(payload, separators=(',', ':')).encode() + b'\n\0'
        packet = HEADER.pack(255, 0, self.session, self.sequence, 0, 0, mid, len(body)) + body
        self.sequence = (self.sequence + 1) & 0xffffffff
        sent = self.sdk.vv_nat_send(self.cid, packet, len(packet))
        # vv_nat_send counts its own 12-byte transport envelope as well.
        if sent < len(packet):
            raise ConnectionError('Не удалось отправить команду регистратору')

    def receive(self, timeout=1):
        deadline = time.monotonic() + timeout
        while True:
            if self.overflow.is_set():
                raise ConnectionError('Переполнен буфер P2P')
            if len(self.buffer) >= HEADER.size:
                fields = HEADER.unpack_from(self.buffer)
                size = fields[-1]
                if fields[0] != 255 or size > 8 * 1024 * 1024:
                    raise ConnectionError('Неверный пакет DVRIP')
                if len(self.buffer) >= HEADER.size + size:
                    body = bytes(self.buffer[HEADER.size:HEADER.size + size])
                    del self.buffer[:HEADER.size + size]
                    if self.session and fields[2] != self.session:
                        continue
                    return fields, body
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty()
            data = self.events.get(timeout=remaining)
            if isinstance(data, tuple):
                raise TransportClosed('P2P-соединение закрыто: %d' % data[1])
            self.buffer.extend(data)

    def request(self, mid, payload, timeout=10):
        self.send(mid, payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fields, body = self.receive(max(.1, deadline - time.monotonic()))
            except queue.Empty:
                raise RequestTimeout(mid) from None
            if fields[-2] == mid + 1:
                reply = json.loads(body.rstrip(b'\n\0'))
                if reply.get('Ret') not in (100, 515):
                    raise ConnectionError('Регистратор отклонил команду: %s' % reply.get('Ret'))
                return fields, reply
        raise RequestTimeout(mid)

    def login(self, config, timeout=10):
        digest = hashlib.md5(config['PASSWORD'].encode()).digest()
        chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
        password = ''.join(chars[(digest[i] + digest[i + 1]) % 62] for i in range(0, 16, 2))
        fields, reply = self.request(1000, {'EncryptType': 'MD5', 'LoginType': 'DVRIP-Web',
                                          'UserName': config['USERNAME'], 'PassWord': password}, timeout=timeout)
        self.session = fields[2]
        return reply

    def reopen(self, serial):
        # A monitor socket cannot be claimed twice on this firmware. Keep the
        # initialized P2P client but replace its device connection, then log in.
        old, self.cid = self.cid, 0
        self.sdk.vv_nat_close(old)
        with self.events.mutex:
            self.events.queue.clear()
        self.buffer.clear()
        self.overflow.clear()
        self.session = self.sequence = 0
        self.connect_device(serial)

    def command(self, name):
        return {'Name': name, 'SessionID': '0x%08x' % self.session}

    def monitor(self, channel, action, mid):
        payload = self.command('OPMonitor')
        payload['OPMonitor'] = {'Action': action, 'Parameter': {
            'Channel': channel, 'CombinMode': 'CONNECT_ALL',
            'StreamType': 'Extra1', 'TransMode': 'TCP'}}
        if action in ('Start', 'Stop'):
            self.send(mid, payload)
            return
        return self.request(mid, payload)

    def close(self):
        if self.cid > 0:
            self.sdk.vv_nat_close(self.cid)
            self.cid = 0
        self.sdk.vv_nat_exit()


class Frames:
    """Incremental DVRIP media framing; output Annex B, ignore audio/telemetry."""
    def __init__(self):
        self.buffer = bytearray()
        self.ready = False
        self.codec = None
        self.fps = 12
        self.keyframes = 0

    def feed(self, data):
        self.buffer.extend(data)
        output = bytearray()
        cursor = 0
        size = len(self.buffer)
        while cursor + 8 <= size:
            marker = self.buffer.find(b'\0\0\1', cursor)
            if marker < 0:
                cursor = max(cursor, size - 3)
                break
            kind = self.buffer[marker + 3]
            header = 16 if kind in (0xfc, 0xfe) else 8
            if kind not in (0xfc, 0xfe, 0xfd, 0xfa, 0xf9):
                cursor = marker + 4
                continue
            if marker + header > size:
                cursor = marker
                break
            length = (struct.unpack_from('<I', self.buffer, marker + 12)[0] if header == 16 else
                      struct.unpack_from('<I', self.buffer, marker + 4)[0] if kind == 0xfd else
                      struct.unpack_from('<H', self.buffer, marker + 6)[0])
            if length > 8 * 1024 * 1024:
                raise ValueError('Слишком большой видеокадр')
            end = marker + header + length
            if end > size:
                cursor = marker
                break
            if header == 16:
                self.codec = self.buffer[marker + 4] & 15
                self.fps = self.buffer[marker + 5] & 31 or 12
                self.keyframes += 1
                self.ready = True
            if self.ready and kind in (0xfc, 0xfe, 0xfd):
                output.extend(self.buffer[marker + header:end])
            cursor = end
        del self.buffer[:cursor]
        return bytes(output)


class Viewer:
    def __init__(self, config=None):
        self.config = config
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.selected = 0
        self.generation = 0
        self.channels = []
        self.jpeg = None
        self.jpeg_generation = None
        self.frame_id = 0
        self.last_frame = 0
        self.state = 'Подключение к регистратору…'
        self.decoder = None
        self.decoded = 0
        self.decoder_input = None
        self.decoder_failed = threading.Event()
        self.reset_metrics()

    def reset_metrics(self, reused=False):
        with self.condition:
            self.started = time.monotonic()
            self.timings = {}
            self.media_packets = 0
            self.media_bytes = 0
            self.failure = None
            self.transport_reused = reused

    def mark(self, name):
        with self.condition:
            if name not in self.timings:
                self.timings[name] = round((time.monotonic() - self.started) * 1000)
                self.condition.notify_all()

    def update(self, text):
        with self.condition:
            self.state = text
            self.condition.notify_all()

    def select(self, channel):
        with self.condition:
            if channel < 0 or channel >= len(self.channels):
                raise ValueError('Нет такого канала')
            if self.selected != channel:
                self.selected = channel
                self.generation += 1
                self.jpeg = None
                self.jpeg_generation = None
                self.last_frame = 0
                self.state = 'Переключение камеры…'
                self.wakeup.set()
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {'channels': self.channels, 'selected': self.selected, 'generation': self.generation,
                    'server_id': SERVER_ID,
                    'frame_generation': self.jpeg_generation,
                    'state': self.state, 'live': bool(self.jpeg and self.jpeg_generation == self.generation
                                                      and time.monotonic() - self.last_frame < 5),
                    'frames': self.decoded, 'diagnostics': {
                        'timings_ms': dict(self.timings), 'media_packets': self.media_packets,
                        'media_bytes': self.media_bytes, 'failure': self.failure,
                        'transport_reused': self.transport_reused}}

    def start_decoder(self, codec, generation, fps):
        decoder = subprocess.Popen(decoder_command(codec, fps),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.decoder = decoder
        pending = queue.Queue(maxsize=32)
        failed = threading.Event()
        self.decoder_input = pending
        self.decoder_failed = failed
        self.mark('decoder_started')

        def write_video():
            try:
                while decoder.poll() is None and not self.stop.is_set():
                    try:
                        video = pending.get(timeout=.5)
                    except queue.Empty:
                        continue
                    # A pipe may accept a partial write; finish this stream chunk.
                    remaining = memoryview(video)
                    while remaining:
                        count = decoder.stdin.write(remaining)
                        if not count:
                            raise BrokenPipeError()
                        remaining = remaining[count:]
            except (OSError, ValueError):
                failed.set()
        threading.Thread(target=write_video, daemon=True).start()

        def read_jpeg():
            buffer = bytearray()
            try:
                while not self.stop.is_set():
                    data = decoder.stdout.read(65536)
                    if not data:
                        failed.set()
                        break
                    buffer.extend(data)
                    # Parsing only: no per-frame resource calls.
                    cursor = 0
                    latest = None
                    while True:
                        start = buffer.find(b'\xff\xd8', cursor)
                        end = buffer.find(b'\xff\xd9', start + 2) if start >= 0 else -1
                        if end < 0:
                            break
                        latest = bytes(buffer[start:end + 2])
                        cursor = end + 2
                    del buffer[:cursor]
                    if len(buffer) > 8 * 1024 * 1024:
                        raise ValueError('JPEG framing')
                    if latest:
                        with self.condition:
                            if generation == self.generation:
                                self.jpeg = latest
                                self.jpeg_generation = generation
                                self.frame_id += 1
                                self.decoded += 1
                                self.last_frame = time.monotonic()
                                self.mark('first_jpeg')
                                self.state = 'Прямой эфир'
                                self.condition.notify_all()
            except (OSError, ValueError):
                failed.set()
        threading.Thread(target=read_jpeg, daemon=True).start()
        return decoder

    def close_decoder(self):
        if self.decoder:
            # This decoder has no recording to flush. SIGTERM can leave FFmpeg
            # blocked on its input pipe until the old 3-second timeout expires.
            self.decoder.kill()
            self.decoder.wait(timeout=2)
            self.decoder.stdin.close()
            self.decoder.stdout.close()
            self.decoder = None

    def activate(self, transport):
        channel = self.selected
        generation = self.generation
        transport.monitor(channel, 'Claim', 1413)
        self.mark('claimed')
        transport.monitor(channel, 'Start', 1410)
        self.mark('stream_requested')
        self.update('Ожидание первого кадра…')
        return channel, generation, Frames(), time.monotonic()

    def reconnect(self, transport, config):
        transport.reopen(config['SERIAL_NUM'])
        self.mark('p2p_connected')
        try:
            info = transport.login(config, timeout=3)
        except (RequestTimeout, TransportClosed):
            # Discard this device socket: a late response must never be matched
            # to the next login. Retry once before replacing the entire SDK.
            self.mark('device_retry')
            self.update('Повтор подключения к камере…')
            transport.reopen(config['SERIAL_NUM'])
            info = transport.login(config, timeout=3)
        self.mark('authenticated')
        return info

    def run(self):
        config = self.config or credentials()
        while not self.stop.is_set():
            transport = None
            try:
                self.update('Подключение к регистратору…')
                address = discover()
                self.mark('discovered')
                transport = Transport(address, config['SERIAL_NUM'], self.mark)
                self.update('Вход в регистратор…')
                info = transport.login(config)
                self.mark('authenticated')
                self.update('Получение списка камер…')
                _, titles = transport.request(1048, transport.command('ChannelTitle'))
                names = titles.get('ChannelTitle', [])
                self.mark('channels_loaded')
                with self.condition:
                    self.channels = [str(names[i]) if i < len(names) else 'Камера %d' % (i + 1)
                                     for i in range(info['ChannelNum'])]
                self.wakeup.clear()
                channel, generation, frames, stream_started = self.activate(transport)
                keepalive = stream_started
                media_at = stream_started
                decoder = None
                while not self.stop.is_set():
                    if self.wakeup.is_set():
                        self.wakeup.clear()
                        self.reset_metrics(reused=True)
                        self.close_decoder()
                        self.mark('decoder_closed')
                        info = self.reconnect(transport, config)
                        keepalive = time.monotonic()
                        channel, generation, frames, stream_started = self.activate(transport)
                        media_at = stream_started
                        decoder = None
                    now = time.monotonic()
                    if now - keepalive >= min(15, info.get('AliveInterval', 21) - 2):
                        transport.send(1006, transport.command('KeepAlive'))
                        keepalive = now
                    failure = frame_health(now, stream_started, media_at, self.last_frame,
                                           self.decoder_failed.is_set() if decoder else False)
                    if failure:
                        self.failure = failure
                        raise TimeoutError('Нет свежих кадров; восстановление соединения…')
                    try:
                        fields, body = transport.receive(1)
                    except queue.Empty:
                        continue
                    if fields[-2] == 1412:
                        media_at = time.monotonic()
                        self.media_packets += 1
                        self.media_bytes += len(body)
                        self.mark('first_media')
                        video = frames.feed(body)
                        if video:
                            self.mark('first_keyframe')
                            if decoder is None:
                                if frames.codec not in (2, 3):
                                    raise ValueError('Неподдерживаемый видеокодек')
                                decoder = self.start_decoder(frames.codec, generation, frames.fps)
                            try:
                                # A network burst is not a stalled decoder.
                                self.decoder_input.put(video, timeout=.5)
                            except queue.Full:
                                self.failure = 'decoder_backpressure'
                                raise TimeoutError('Декодер перестал принимать видео; переподключение…')
                    elif fields[-2] in (1007, 1411):
                        reply = json.loads(body.rstrip(b'\n\0'))
                        if reply.get('Ret') not in (None, 100, 515):
                            self.failure = 'control_rejected'
                            raise ConnectionError('Регистратор отклонил команду: %s' % reply['Ret'])
            except Exception as error:
                # Native/HTTP responses may contain credentials: never log raw payloads.
                message = str(error) if isinstance(error, (TimeoutError, ConnectionError)) else 'Ошибка соединения; повторная попытка…'
                if not self.failure:
                    self.failure = ('request_timeout_%d' % error.mid if isinstance(error, RequestTimeout)
                                    else type(error).__name__)
                print('Повтор соединения:', type(error).__name__, message, file=OUT, flush=True)
                self.update(message)
            finally:
                self.close_decoder()
                if transport:
                    transport.close()
                with self.condition:
                    self.jpeg = None
                    self.condition.notify_all()
            # Reinitialize the legacy SDK only in a fresh process, even after errors.
            return


class IsolatedViewer(Viewer):
    """The legacy vendor transport lives in a disposable Rosetta process."""
    def __init__(self, config=None):
        super().__init__(config)
        self.child = None
        self.remote_diagnostics = {}
        self.child_started = 0
        self.worker_message = 0
        self.selection_started = 0
        self.termination = None

    def snapshot(self):
        with self.condition:
            result = super().snapshot()
            result['diagnostics'] = dict(self.remote_diagnostics)
            return result

    def select(self, channel):
        previous = self.selected
        super().select(channel)
        if previous != self.selected:
            self.selection_started = time.monotonic()
        if previous != self.selected and self.child is not None:
            try:
                self.child.stdin.write((json.dumps({'channel': channel, 'generation': self.generation}) + '\n').encode())
                self.child.stdin.flush()
            except (OSError, ValueError):
                pass

    def accept_packet(self, meta, jpeg):
        with self.condition:
            if meta['selected'] != self.selected or meta['generation'] != self.generation:
                return False
            if jpeg and meta.get('frame_generation') != self.generation:
                return False
            if meta['channels']:
                self.channels = meta['channels']
            self.state = meta['state']
            self.remote_diagnostics = meta.get('diagnostics', {})
            if jpeg:
                self.jpeg = jpeg
                self.jpeg_generation = self.generation
                self.last_frame = time.monotonic()
                self.frame_id += 1
                self.decoded += 1
            elif not meta['live']:
                self.jpeg = None
                self.jpeg_generation = None
            self.condition.notify_all()
            return True

    def supervise(self):
        # Independent of blocking native calls and the IPC reader.
        while not self.stop.wait(.5):
            child = self.child
            if child is None or child.poll() is not None:
                continue
            now = time.monotonic()
            if self.termination is not None and self.termination[0] is child:
                if now - self.termination[1] > 2:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                continue
            if now - self.worker_message > 6:
                reason = 'worker_heartbeat_timeout'
            elif self.last_frame and now - self.last_frame > 10:
                reason = 'decoded_frames_timeout'
            elif not self.jpeg and now - max(self.child_started, self.selection_started) > 30:
                reason = 'startup_timeout'
            else:
                continue
            diagnostic('watchdog_restart', reason=reason)
            self.termination = (child, now)
            self.update('Восстановление соединения…')
            try:
                child.terminate()
            except ProcessLookupError:
                pass

    def run(self):
        threading.Thread(target=self.supervise, daemon=True).start()
        def read_exact(stream, size):
            data = bytearray()
            while len(data) < size:
                chunk = stream.read(size - len(data))
                if not chunk:
                    raise EOFError()
                data.extend(chunk)
            return data

        while not self.stop.is_set():
            self.wakeup.clear()
            with self.condition:
                self.generation += 1
                channel, generation = self.selected, self.generation
                self.jpeg = None
                self.jpeg_generation = None
            self.child_started = self.worker_message = time.monotonic()
            self.last_frame = 0
            self.termination = None
            self.child = subprocess.Popen([
                '/usr/bin/arch', '-x86_64', '/usr/bin/python3', str(ROOT / 'vesta.py'),
                '--worker', str(channel), str(generation)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            # Covers a selection arriving while Popen was starting.
            self.child.stdin.write((json.dumps({'channel': self.selected, 'generation': self.generation, 'config': self.config}) + '\n').encode())
            self.child.stdin.flush()
            diagnostic('worker_started', channel=channel)
            signature = None
            try:
                while not self.stop.is_set():
                    meta_size, jpeg_size = struct.unpack('<II', read_exact(self.child.stdout, 8))
                    if meta_size > 65536 or jpeg_size > 8 * 1024 * 1024:
                        raise ValueError('Worker framing')
                    meta = json.loads(read_exact(self.child.stdout, meta_size))
                    self.worker_message = time.monotonic()
                    jpeg = bytes(read_exact(self.child.stdout, jpeg_size)) if jpeg_size else None
                    if self.accept_packet(meta, jpeg):
                        next_signature = (self.state, tuple(self.remote_diagnostics.get('timings_ms', {})))
                        if signature != next_signature:
                            diagnostic('stage', channel=self.selected, state=self.state,
                                       diagnostics=self.remote_diagnostics)
                            signature = next_signature
            except (EOFError, OSError, ValueError):
                self.update('Восстановление соединения…')
            finally:
                if self.child.poll() is None:
                    self.child.terminate()
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
                self.child.stdout.close()
                self.child.stdin.close()
                diagnostic('worker_stopped', channel=channel, exit_code=self.child.returncode)
                self.child = None
                with self.condition:
                    self.jpeg = None
            if not self.stop.is_set() and not self.wakeup.is_set():
                self.wakeup.wait(1)


def worker_main():
    global OUT
    pipe = os.fdopen(os.dup(1), 'wb', buffering=65536)
    OUT = os.fdopen(os.dup(2), 'w', buffering=1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    initial = json.loads(sys.stdin.buffer.readline(16384))
    viewer = Viewer(initial.get('config'))
    viewer.selected = int(sys.argv[2])
    viewer.generation = int(sys.argv[3])
    def stop(*_):
        viewer.stop.set()
        viewer.wakeup.set()
        with viewer.condition:
            viewer.condition.notify_all()
    signal.signal(signal.SIGTERM, stop)

    def commands():
        # One command stream, no camera-specific cloud connections.
        while not viewer.stop.is_set():
            line = sys.stdin.buffer.readline(128)
            if not line:
                stop()
                return
            try:
                command = json.loads(line)
                channel = command['channel']
                generation = command['generation']
                if type(channel) is not int or not 0 <= channel < 256:
                    continue
                if type(generation) is not int or generation < 0:
                    continue
                with viewer.condition:
                    if generation > viewer.generation:
                        viewer.selected = channel
                        viewer.generation = generation
                        viewer.jpeg = None
                        viewer.jpeg_generation = None
                        viewer.last_frame = 0
                        viewer.state = 'Переключение камеры…'
                        viewer.wakeup.set()
                        viewer.condition.notify_all()
            except (ValueError, KeyError):
                continue
    threading.Thread(target=commands, daemon=True).start()

    def publish():
        last = -1
        last_state = None
        last_generation = -1
        last_timing_count = -1
        while not viewer.stop.is_set():
            with viewer.condition:
                viewer.condition.wait_for(lambda: viewer.stop.is_set() or viewer.frame_id != last or
                                          viewer.state != last_state or viewer.generation != last_generation or
                                          len(viewer.timings) != last_timing_count, timeout=1)
                meta = json.dumps(viewer.snapshot(), ensure_ascii=False).encode()
                jpeg = viewer.jpeg if viewer.jpeg and viewer.frame_id != last else b''
                last = viewer.frame_id
                last_state = viewer.state
                last_generation = viewer.generation
                last_timing_count = len(viewer.timings)
            try:
                packet = struct.pack('<II', len(meta), len(jpeg)) + meta + jpeg
                pipe.write(packet)
                pipe.flush()
            except BrokenPipeError:
                stop()
                return
    threading.Thread(target=publish, daemon=True).start()
    try:
        viewer.run()
    finally:
        # Native callback threads must not enter Python during interpreter
        # finalization. The parent owns restart and this process is disposable.
        os._exit(0)


def catalog_main():
    # Silence native output before loading the library; return only the catalog.
    pipe = os.fdopen(os.dup(1), 'w', buffering=1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    try:
        config = json.loads(sys.stdin.buffer.readline(16384))
        transport = Transport(discover(), config['SERIAL_NUM'])
        info = transport.login(config)
        _, result = transport.request(1048, transport.command('ChannelTitle'))
        names = result.get('ChannelTitle', [])
        count = max(0, min(int(info['ChannelNum']), 256))
        pipe.write(json.dumps([str(names[i])[:120] if i < len(names) else 'Камера %d' % (i+1)
                               for i in range(count)], ensure_ascii=False) + '\n')
        pipe.flush()
        os._exit(0)
    except Exception:
        os._exit(1)


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker_main()
    elif '--catalog' in sys.argv:
        catalog_main()
