"""One disposable native SDK process for the complete camera set.

Only compressed, bounded media and explicit status fields cross stdout IPC.
Credentials enter stdin; native output is silenced before loading the SDK.
"""
import json
from collections import deque
import os
import queue
import signal
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

from .audio import AudioClock
from .connections import ConnectionGate, ControlSlot
from .motion import MotionMonitor
from .event_log import EventJournal
from ..paths import ROOT
from .session import SharedSession, SessionChanged
from .mp4 import Muxer, parameter_sets, ReorderedStream
from .vesta import Transport, Frames, discover


class Outbox:
    def __init__(self, limit=8*1024*1024):
        self.limit, self.size = limit, 0
        self.condition = threading.Condition()
        self.items = deque()

    def put(self, meta, data=b'', timeout=2):
        header = json.dumps(meta, separators=(',', ':'), ensure_ascii=False).encode()
        packet = struct.pack('<II', len(header), len(data)) + header + data
        with self.condition:
            if not self.condition.wait_for(lambda: self.size+len(packet)<=self.limit, timeout=timeout):
                if timeout == 0:
                    return False
                raise TimeoutError('IPC backpressure')
            self.items.append(packet)
            self.size += len(packet)
            self.condition.notify_all()

    def get(self):
        with self.condition:
            self.condition.wait_for(lambda: bool(self.items))
            packet = self.items.popleft()
            self.size -= len(packet)
            self.condition.notify_all()
            return packet


def reconcile_channels(channels, selected, create):
    # Stop is recorder-wide for this channel within the shared SessionID.
    # A replacement must wait until the old thread has sent its final Stop.
    finished=[]
    for cid, channel in channels.items():
        if cid not in selected or selected[cid]!=channel.token:
            channel.stop.set()
        if not channel.thread.is_alive():
            finished.append(cid)
    for cid in finished:
        del channels[cid]
    for cid in selected.keys()-channels.keys():
        channels[cid]=create(cid,selected[cid])


class Channel:
    def __init__(self, cid, token, hub, config, outbox, connecting, initializing, session):
        self.cid, self.hub, self.config = cid, hub, config
        self.token = token
        self.outbox, self.connecting, self.initializing = outbox, connecting, initializing
        self.session=session
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        delay = 1
        while not self.stop.is_set():
            transport = None
            monitoring=False
            generation = uuid.uuid4().hex
            base = dict(cid=self.cid, token=self.token, generation=generation)
            stage = 'connect'
            try:
                self.outbox.put(dict(base, type='status', state='Подключение к камере…'))
                lease=self.session.wait(self.stop)
                if lease is None:return
                # The SDK requires a data socket per channel; authentication
                # and monitor control belong to the shared recorder session.
                with self.connecting.slot(self.cid):
                    if self.stop.is_set():
                        return
                    transport = Transport(None, self.config['SERIAL_NUM'], hub=self.hub)
                    transport.event_channel=self.cid
                    transport.session=lease.transport.session
                    stage = 'claim'
                    transport.monitor(self.cid-1, 'Claim', 1413)
                    self.session.send(lease,1410,transport.monitor_command(self.cid-1,'Start'))
                    monitoring=True
                stage = 'media'
                frames, muxer, audio_clock = Frames(), None, AudioClock()
                init_reason='connect'
                started = media_at = time.monotonic()
                while not self.stop.is_set():
                    now = time.monotonic()
                    if not self.session.is_current(lease):raise SessionChanged()
                    if now-media_at > (15 if muxer else 30) or (not muxer and now-started>35):
                        raise TimeoutError('Media timeout')
                    try:
                        fields, body = transport.receive(.5)
                    except queue.Empty:
                        continue
                    if fields[-2] != 1412:
                        continue
                    # Parse a bulk packet before processing its access units.
                    audio = []
                    units = frames.feed_units(body, audio)
                    for video, key, codec, fps in units:
                        params = parameter_sets(video, codec) if key else ()
                        if muxer and (codec != muxer.codec or fps != muxer.fps or
                                      (params and params != muxer.parameters)):
                            init_reason='codec' if codec!=muxer.codec else 'fps' if fps!=muxer.fps else 'parameters'
                            muxer = None
                            audio_clock = AudioClock()
                            generation = uuid.uuid4().hex
                            base = dict(cid=self.cid, token=self.token, generation=generation)
                        if muxer is None:
                            if not key:
                                continue
                            stage = 'initialization'
                            init_at=time.monotonic()
                            with self.initializing:
                                muxer = Muxer(video, codec, fps)
                            self.outbox.put(dict(base, type='init', mime=muxer.mime, fps=fps,
                                reason=init_reason,initialization_ms=round((time.monotonic()-init_at)*1000)), muxer.init)
                            stage = 'media'
                        fragment = muxer.fragment(video, key)
                        self.outbox.put(dict(base, type='media', key=key,worker_at=time.time(),sdk_gap=transport.max_callback_gap), fragment)
                        media_at = time.monotonic()
                        delay = 1
                    if muxer and audio:
                        encoded = audio_clock.batch(audio, muxer.timestamp/90000)
                        if encoded:
                            self.outbox.put(dict(base, type='audio'), encoded, timeout=0)
            except Exception as error:
                # No exception text, native payload, credentials or endpoint IDs.
                try:
                    self.outbox.put(dict(base, type='status', stage=stage,
                        error=type(error).__name__, state=(
                            'Этот поток требует поддержки B-кадров. Обратитесь к администратору.'
                            if isinstance(error,ReorderedStream) else 'Соединение прервано. Повтор подключения…')))
                except TimeoutError:
                    return
            finally:
                if transport:
                    if monitoring and self.session.is_current(lease):
                        try:self.session.send(lease,1410,transport.monitor_command(self.cid-1,'Stop'))
                        except (ConnectionError,OSError):pass
                    transport.close()
            self.stop.wait(delay)
            delay = min(30, delay*2)


def main():
    pipe = os.fdopen(os.dup(1), 'wb', buffering=0)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    initial = json.loads(sys.stdin.buffer.readline(65536))
    config = initial['config']
    outbox = Outbox()
    stopping = threading.Event()
    desired = {cid:initial.get('tokens',{}).get(str(cid),'probe') for cid in initial.get('camera_ids', [])}
    condition = threading.Condition()
    channels = {}
    connecting, initializing = ConnectionGate(2), threading.Semaphore(1)
    connecting.prioritize(initial.get('priority_ids', []))

    def stop(*_):
        stopping.set()
        with condition:
            condition.notify_all()
    signal.signal(signal.SIGTERM, stop)

    def publish():
        try:
            while not stopping.is_set():
                packet = memoryview(outbox.get())
                while packet:
                    written = pipe.write(packet)
                    if not written:
                        raise BrokenPipeError()
                    packet = packet[written:]
        except (OSError, ValueError):
            stop()
    threading.Thread(target=publish, daemon=True).start()

    def commands():
        nonlocal desired
        while not stopping.is_set():
            line = sys.stdin.buffer.readline(65536)
            if not line:
                stop()
                return
            try:
                update=json.loads(line)
                ids = update['camera_ids']
                if not isinstance(ids,list) or len(ids)>256 or any(type(cid) is not int or not 1<=cid<=256 for cid in ids):
                    raise ValueError()
                with condition:
                    priority=update.get('priority_ids',[])
                    selected_ids=set(ids)
                    if not isinstance(priority,list) or len(priority)>256 or any(type(cid) is not int or cid not in selected_ids for cid in priority):
                        raise ValueError()
                    connecting.prioritize(priority)
                    tokens = update.get('tokens',{})
                    desired = {cid:tokens.get(str(cid),'probe') for cid in ids}
                    condition.notify_all()
            except (ValueError, KeyError, TypeError):
                stop()
    threading.Thread(target=commands, daemon=True).start()
    hub = session = journal = None
    try:
        data_dir=Path(os.environ.get('DATA_DIR',ROOT/'.runtime/data'))
        try:
            data_dir.mkdir(parents=True,exist_ok=True)
            journal=EventJournal(data_dir/'recorder-events.jsonl',config)
        except (OSError,RuntimeError):
            pass  # Diagnostics must not prevent live video from starting.
        hub = Transport(discover(), None)
        hub.event_journal=journal
        session=SharedSession(hub,config,stopping,connecting,outbox)
        session.start()
        monitor = MotionMonitor(hub, config, outbox, stopping, ControlSlot(connecting),session=session)
        threading.Thread(target=monitor.run, daemon=True).start()
        offline_since = None
        while not stopping.is_set():
            if not session.thread.is_alive():
                raise ConnectionError('Recorder session owner stopped')
            if hub.sdk.vv_nat_get_status() != 1:
                offline_since = offline_since or time.monotonic()
                if time.monotonic()-offline_since > 20:
                    raise ConnectionError('P2P unavailable')
            else:
                offline_since = None
            with condition:
                selected = desired.copy()
            reconcile_channels(channels,selected,lambda cid,token:
                Channel(cid,token,hub,config,outbox,connecting,initializing,session))
            outbox.put(dict(type='heartbeat',event_log=journal.stats() if journal else dict(write_failures=1)))
            with condition:
                condition.wait(timeout=1)
    except Exception as error:
        outbox.put(dict(type='worker_error', error=type(error).__name__))
        time.sleep(.1)
    finally:
        # Parent SIGKILL fallback bounds shutdown, including any stuck vendor call.
        stopping.set()
        connecting.close()
        for channel in channels.values():
            channel.stop.set()
        if session:session.close()
        if hub:
            hub.close()
        if journal:journal.close()
        os._exit(0)


if __name__ == '__main__':
    main()
