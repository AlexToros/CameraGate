"""Recorder motion notifications, with one bulk state reconciliation."""
import json
import queue
import re
import threading
import time

from .vesta import Transport


class MotionState:
    def __init__(self):
        self.video = set()
        self.human = {}

    @property
    def active(self):
        # A missed Stop must not leave the person icon active indefinitely;
        # new events refresh its bounded lifetime.
        now = time.monotonic()
        self.human = {cid:until for cid,until in self.human.items() if until>now}
        return self.video | self.human.keys()

    def alarm(self, message):
        alarm = message.get('AlarmInfo', message)
        if not isinstance(alarm, dict) or alarm.get('Event') not in ('VideoMotion','appEventHumanDetectAlarm'):
            return False
        channel, status = alarm.get('Channel'), alarm.get('Status')
        if isinstance(channel,str) and channel.isascii() and channel.isdigit() and len(channel)<=3:
            channel=int(channel)
        if type(channel) is not int or not 0 <= channel < 256:
            return False
        if status not in ('Start','Stop'):
            return False
        if alarm['Event']=='appEventHumanDetectAlarm':
            if status=='Start':self.human[channel+1]=time.monotonic()+10
            else:self.human.pop(channel+1,None)
        elif status == 'Start':
            self.video.add(channel+1)
        elif status == 'Stop':
            self.video.discard(channel+1)
        return True

    def reconcile(self, message):
        value = message['WorkState']['AlarmState']['VideoMotion']
        mask = int(value, 16) if isinstance(value, str) else value
        if type(mask) is not int or not 0 <= mask < 1 << 256:
            raise ValueError('Invalid motion mask')
        self.video = {index+1 for index in range(mask.bit_length()) if mask & (1 << index)}


class MotionMonitor:
    """One control connection for all cameras; failure never stops media."""
    def __init__(self, hub, config, outbox, stopping, connecting):
        self.hub, self.config, self.outbox = hub, config, outbox
        self.stopping, self.connecting = stopping, connecting
        self.diagnostics=dict(notifications=0,motion_events=0,other_notifications=0,last_notice_id=None,state_checks=0,failures=0,last_event=None,last_event_type=None,last_event_channel=None,last_event_status=None)

    def publish(self, state=None, method=None, error=None):
        self.outbox.put(dict(type='motion', available=state is not None,
            camera_ids=sorted(state.active) if state else [],method=method,error=error,diagnostics=self.diagnostics.copy()), timeout=0)

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
                        self.diagnostics['foreign_alarm_messages']=getattr(transport,'foreign_alarm_messages',0)
                        continue
                    mid = fields[-2]
                    if mid not in (1501, 1504, 1021):
                        if 1500<=mid<=1510:
                            self.diagnostics['other_notifications']+=1
                            self.diagnostics['last_notice_id']=mid
                        continue
                    message = json.loads(body.rstrip(b'\n\0'))
                    if mid == 1501:
                        subscribed = message.get('Ret') == 100
                    elif mid == 1504:
                        self.diagnostics['notifications']+=1
                        self.diagnostics['last_event']=time.time()
                        alarm=message.get('AlarmInfo',message)
                        if isinstance(alarm,dict):
                            kind=alarm.get('Event')
                            self.diagnostics['last_event_type']=kind if isinstance(kind,str) and re.fullmatch('[A-Za-z_]{1,48}',kind) else 'other'
                            channel=alarm.get('Channel')
                            if isinstance(channel,str) and channel.isascii() and channel.isdigit() and len(channel)<=3:channel=int(channel)
                            self.diagnostics['last_event_channel']=channel+1 if type(channel) is int and 0<=channel<256 else None
                            self.diagnostics['last_event_status']=alarm.get('Status') if alarm.get('Status') in ('Start','Stop') else 'other'
                        if state.alarm(message):self.diagnostics['motion_events']+=1
                    elif message.get('Ret') == 100:
                        state.reconcile(message)
                        self.diagnostics['state_checks']+=1
                        known, pending = True, False
                    else:
                        raise ConnectionError('Motion state unavailable')
                    if known:
                        self.publish(state,method='events' if subscribed else 'poll')
                        delay = 2
            except Exception as error:
                self.diagnostics['failures']+=1
                self.publish(error=type(error).__name__)
            finally:
                if transport:
                    transport.close()
            self.stopping.wait(delay)
            delay = min(30, delay*2)
