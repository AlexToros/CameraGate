"""One authenticated recorder session shared by live media and event control."""
from dataclasses import dataclass
import json
import queue
import threading
import time

from .vesta import Transport, RequestTimeout


class SessionChanged(ConnectionError):
    pass


@dataclass(frozen=True)
class Lease:
    transport: object
    info: dict
    generation: int


class ControlListener:
    def __init__(self, owner, lease):
        self.owner, self.lease = owner, lease
        self.messages = queue.Queue(maxsize=256)
        self.overflow = threading.Event()

    def login(self, config, timeout=5):
        return self.lease.info

    def command(self, name):
        return self.lease.transport.command(name)

    def send(self, mid, payload):
        if mid != 1006:  # Keepalive belongs to the shared session owner.
            self.owner.send(self.lease, mid, payload)

    def receive(self, timeout):
        if self.overflow.is_set():
            raise ConnectionError('Motion queue overflow')
        if not self.owner.is_current(self.lease):
            raise SessionChanged()
        return self.messages.get(timeout=timeout)

    @property
    def foreign_alarm_messages(self):
        return self.lease.transport.foreign_alarm_messages

    def close(self):
        with self.owner.condition:
            if self.owner.listener is self:
                self.owner.listener = None


class SharedSession:
    def __init__(self, hub, config, stopping, connecting, outbox):
        self.hub, self.config, self.stopping = hub, config, stopping
        self.connecting, self.outbox = connecting, outbox
        self.condition = threading.Condition()
        self.current = self.listener = None
        self.closed = False
        self.generation = self.logins = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def wait(self, stopped):
        with self.condition:
            while not self.closed and not self.stopping.is_set() and not stopped.is_set():
                if self.current is not None:
                    return self.current
                self.condition.wait(timeout=.25)
        return None

    def is_current(self, lease):
        return not self.closed and not self.stopping.is_set() and lease is not None and self.current is lease

    def send(self, lease, mid, payload):
        with self.condition:
            if not self.is_current(lease):
                raise SessionChanged()
            lease.transport.send(mid, payload)

    def listen(self, stopped):
        lease = self.wait(stopped)
        if lease is None:
            raise SessionChanged()
        listener = ControlListener(self, lease)
        with self.condition:
            if not self.is_current(lease):
                raise SessionChanged()
            self.listener = listener
        return listener

    def publish(self, ready, error=None):
        self.outbox.put(dict(type='session',ready=ready,logins=self.logins,
            generation=self.generation,error=error),timeout=0)

    def run(self):
        delay = 2
        while not self.closed and not self.stopping.is_set():
            transport = None
            try:
                with self.connecting.slot(None):
                    if self.closed or self.stopping.is_set():
                        return
                    transport = Transport(None,self.config['SERIAL_NUM'],hub=self.hub)
                    try:
                        info = transport.login(self.config,timeout=5)
                    except RequestTimeout:
                        transport.reopen(self.config['SERIAL_NUM'])
                        info = transport.login(self.config,timeout=5)
                self.generation += 1
                self.logins += 1
                lease = Lease(transport,info,self.generation)
                with self.condition:
                    self.current = lease
                    self.condition.notify_all()
                self.publish(True)
                delay = 2
                keepalive = time.monotonic()
                pending = None
                alive = max(5,min(60,info.get('AliveInterval',21)))
                while not self.closed and not self.stopping.is_set():
                    now = time.monotonic()
                    if pending is not None and now-pending > 2*alive:
                        raise TimeoutError('Recorder keepalive unavailable')
                    if now-keepalive >= max(2,min(15,alive-2)):
                        self.send(lease,1006,transport.command('KeepAlive'))
                        if pending is None:pending = now
                        keepalive = now
                    try:
                        fields,body = transport.receive(.25)
                    except queue.Empty:
                        continue
                    mid = fields[-2]
                    if mid == 1007:
                        reply = json.loads(body.rstrip(b'\n\0'))
                        if reply.get('Ret') not in (100,515):
                            raise ConnectionError('Recorder session expired')
                        pending = None
                        self.publish(True)
                    elif mid in (1501,1504,1021) and len(body)<=65536:
                        listener = self.listener
                        if listener is not None and listener.lease is lease:
                            try:listener.messages.put_nowait((fields,body))
                            except queue.Full:listener.overflow.set()
            except Exception as error:
                self.publish(False,type(error).__name__)
            finally:
                with self.condition:
                    self.current = self.listener = None
                    self.condition.notify_all()
                if transport:
                    try:
                        transport.close()
                    except Exception as error:
                        self.publish(False,type(error).__name__)
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.stopping.is_set(),timeout=delay)
            delay = min(30,delay*2)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        # Queue wakeup only; the owner closes its own native socket.
        lease=self.current
        if lease is not None:
            lease.transport.enqueue(('closed',0))
        if self.thread.is_alive():
            self.thread.join(timeout=1)
