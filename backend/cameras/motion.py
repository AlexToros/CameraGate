"""Recorder motion notifications, with one bulk state reconciliation."""
import json
import queue
import threading
import time

from .vesta import Transport


class MotionState:
    def __init__(self):
        self.active = set()

    def alarm(self, message):
        alarm = message.get('AlarmInfo', message)
        if not isinstance(alarm, dict) or alarm.get('Event') != 'VideoMotion':
            return
        channel, status = alarm.get('Channel'), alarm.get('Status')
        if type(channel) is not int or not 0 <= channel < 256:
            return
        if status == 'Start':
            self.active.add(channel+1)
        elif status == 'Stop':
            self.active.discard(channel+1)

    def reconcile(self, message):
        value = message['WorkState']['AlarmState']['VideoMotion']
        mask = int(value, 16) if isinstance(value, str) else value
        if type(mask) is not int or not 0 <= mask < 1 << 256:
            raise ValueError('Invalid motion mask')
        self.active = {index+1 for index in range(mask.bit_length()) if mask & (1 << index)}


class MotionMonitor:
    """One control connection for all cameras; failure never stops media."""
    def __init__(self, hub, config, outbox, stopping, connecting):
        self.hub, self.config, self.outbox = hub, config, outbox
        self.stopping, self.connecting = stopping, connecting

    def publish(self, state=None, method=None, error=None):
        self.outbox.put(dict(type='motion', available=state is not None,
            camera_ids=sorted(state.active) if state else [],method=method,error=error), timeout=0)

    def run(self):
        delay = 2
        while not self.stopping.is_set():
            transport = None
            try:
                with self.connecting:
                    if self.stopping.is_set():
                        return
                    transport = Transport(None, self.config['SERIAL_NUM'], hub=self.hub)
                    info = transport.login(self.config, timeout=5)
                state = MotionState()
                # Guard subscribes this session to notifications. It does not
                # enable detection or modify any recorder configuration.
                transport.send(1500, transport.command(''))
                subscribed, queried, keepalive = False, 0, time.monotonic()
                known = False
                pending = False
                while not self.stopping.is_set():
                    now = time.monotonic()
                    if now-keepalive >= max(2, min(15, info.get('AliveInterval',21)-2)):
                        transport.send(1006, transport.command('KeepAlive'))
                        keepalive = now
                    if pending and now-queried >= 5:
                        raise TimeoutError('Motion state timeout')
                    if not pending and now-queried >= (5 if subscribed else 1):
                        transport.send(1020, transport.command('WorkState'))
                        pending, queried = True, now
                    try:
                        fields, body = transport.receive(.25)
                    except queue.Empty:
                        continue
                    mid = fields[-2]
                    if mid not in (1501, 1504, 1021):
                        continue
                    message = json.loads(body.rstrip(b'\n\0'))
                    if mid == 1501:
                        subscribed = message.get('Ret') == 100
                    elif mid == 1504:
                        state.alarm(message)
                    elif message.get('Ret') == 100:
                        state.reconcile(message)
                        known, pending = True, False
                    else:
                        raise ConnectionError('Motion state unavailable')
                    if known:
                        self.publish(state,method='events' if subscribed else 'poll')
                        delay = 2
            except Exception as error:
                self.publish(error=type(error).__name__)
            finally:
                if transport:
                    transport.close()
            self.stopping.wait(delay)
            delay = min(30, delay*2)
