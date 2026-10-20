"""Isolated, on-demand recorder archive. No recordings are written to disk."""
from datetime import datetime, timedelta
import json
import os
import queue
import signal
import struct
import sys
import time

from .mp4 import Muxer, parameter_sets
from .vesta import Transport, Frames, discover, RequestTimeout

FORMAT = '%Y-%m-%d %H:%M:%S'


def login(transport, config):
    try:
        return transport.login(config,timeout=8)
    except RequestTimeout as error:
        if error.mid!=1000:
            raise
        # A timed-out DVRIP reply cannot be matched safely to a later login.
        # Keep the initialized P2P hub, but replace the device socket once.
        transport.reopen(config['SERIAL_NUM'])
        return transport.login(config,timeout=8)


def frame_time(value):
    try:
        return datetime(2000+(value>>26), (value>>22)&15, (value>>17)&31,
            (value>>12)&31, (value>>6)&63, value&63)
    except (TypeError,ValueError):
        return None


def search(transport, request):
    date = datetime.strptime(request['date'], '%Y-%m-%d')
    begin = request.get('cursor') or date.strftime(FORMAT)
    end = (date+timedelta(days=1)-timedelta(seconds=1)).strftime(FORMAT)
    payload = transport.command('OPFileQuery')
    payload['OPFileQuery'] = dict(BeginTime=begin, EndTime=end, Channel=request['cid']-1,
        DriverTypeMask='0x0000FFFF', Event='*', StreamType='0x00000000', Type='h264')
    _, response = transport.request(1440, payload, timeout=8, accepted=(100,110,111,109,119))
    files = response.get('OPFileQuery') or []
    if not isinstance(files, list) or len(files)>512:
        raise ValueError('Invalid archive list')
    output = []
    seen = set()
    for item in files:
        name, start, stop = item.get('FileName'), item.get('BeginTime'), item.get('EndTime')
        if not isinstance(name,str) or not 1<=len(name)<=1024 or name in seen:
            continue
        first, last = datetime.strptime(start,FORMAT), datetime.strptime(stop,FORMAT)
        if first>last or last<date or first>=date+timedelta(days=1):
            continue
        seen.add(name)
        output.append(dict(file=name, start=start, end=stop))
    output.sort(key=lambda item:(item['start'],item['file']))
    # One bounded device query per page. Overlapping boundary records are
    # deduplicated by the portal; never loop on an unchanging recorder cursor.
    following = max((item['start'] for item in output), default=begin)
    more = (len(files)>=64 or response['Ret']==111) and following>begin and following<end
    return dict(files=output, cursor=following if more else None)


def playback(transport, request, emit):
    entry = request['entry']
    start = datetime.strptime(entry['start'],FORMAT)+timedelta(seconds=request.get('offset',0))
    payload = transport.command('OPPlayBack')
    params = dict(PlayMode='ByName', FileName=entry['file'], Channel=entry['cid']-1,
        StreamType=0, Value=0, TransMode='TCP')
    limit=request.get('end_offset')
    end=datetime.strptime(entry['start'],FORMAT)+timedelta(seconds=limit) if limit is not None else datetime.strptime(entry['end'],FORMAT)
    operation = dict(Parameter=params, StartTime=start.strftime(FORMAT), EndTime=end.strftime(FORMAT))
    payload['OPPlayBack'] = dict(operation, Action='Claim')
    transport.request(1424,payload,timeout=8)
    payload['OPPlayBack'] = dict(operation, Action='Start')
    transport.send(1420,payload)
    frames, muxer = Frames(), None
    started = last = keepalive = time.monotonic()
    origin=datetime.strptime(entry['start'],FORMAT)
    position=request.get('offset',0)
    complete=False
    try:
        while True:
            now = time.monotonic()
            if now-keepalive>=10:
                transport.send(1006,transport.command('KeepAlive'))
                keepalive=now
            if now-last>20 or (not muxer and now-started>30):
                raise TimeoutError('Archive media timeout')
            try:
                fields, body = transport.receive(.5)
            except queue.Empty:
                continue
            mid = fields[-2]
            if mid == 1423:
                break
            if mid == 1421:
                reply=json.loads(body.rstrip(b'\n\0'))
                if reply.get('Ret',100)!=100:
                    raise ConnectionError('Archive rejected')
                if reply.get('OPPlayBack',{}).get('Action')=='EOF':
                    break
                continue
            if mid not in (1422,1426):
                continue
            if not body:
                break
            last=now
            key_times={}
            units=frames.feed_units(body,key_times=key_times)
            for index,(video,key,codec,fps) in enumerate(units):
                actual=frame_time(key_times.get(index)) if key else None
                if actual:
                    position=(actual-origin).total_seconds()
                if limit is not None and position>=limit:
                    complete=True
                    break
                params=parameter_sets(video,codec) if key else ()
                if muxer is None:
                    if not key:
                        continue
                    muxer=Muxer(video,codec,fps)
                    emit(dict(type='init',mime=muxer.mime,
                        offset=max(0,(actual-origin).total_seconds()) if actual else request.get('offset',0)),muxer.init)
                elif codec!=muxer.codec or fps!=muxer.fps or (params and params!=muxer.parameters):
                    raise ValueError('Archive codec changed')
                emit(dict(type='media'),muxer.fragment(video,key))
                position+=1/fps
            if complete:
                break
    finally:
        payload['OPPlayBack']=dict(operation,Action='Stop')
        try:
            transport.send(1420,payload)
        except Exception:
            pass
    if muxer is None:
        raise ValueError('No archive frames')
    emit(dict(type='end'))


def main():
    pipe=os.fdopen(os.dup(1),'wb',buffering=0)
    null=os.open(os.devnull,os.O_WRONLY)
    os.dup2(null,1);os.dup2(null,2);os.close(null)
    class StopRequested(BaseException):
        pass
    def stop(*_):
        raise StopRequested()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    def emit(meta,data=b''):
        header=json.dumps(meta,separators=(',',':')).encode()
        packet=memoryview(struct.pack('<II',len(header),len(data))+header+data)
        while packet:
            written=pipe.write(packet)
            if not written:
                raise BrokenPipeError()
            packet=packet[written:]
    transport=None
    stage='connect'
    try:
        request=json.loads(sys.stdin.buffer.readline(16384))
        config=request.pop('config')
        transport=Transport(discover(),config['SERIAL_NUM'])
        stage='login'
        login(transport,config)
        stage=request['action']
        if request['action']=='search':
            emit(dict(type='result',**search(transport,request)))
        elif request['action']=='play':
            playback(transport,request,emit)
        else:
            raise ValueError('Unknown action')
    except Exception as error:
        emit(dict(type='error',code='denied' if isinstance(error,ConnectionError) and str(error).endswith(': 107') else 'unavailable',
            error_type=type(error).__name__,command=getattr(error,'mid',None),stage=stage))
    finally:
        if transport:
            transport.close()
        os._exit(0)


if __name__=='__main__':
    main()
