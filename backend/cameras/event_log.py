"""Private, bounded recorder control journal. Media and authentication are excluded."""
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading


PRIVATE_KEYS = {'session', 'sessionid', 'username', 'user', 'password', 'passwd',
    'pass', 'token', 'accesstoken', 'authorization', 'communicatekey', 'secret',
    'encryptkey', 'serialnumber', 'serialnum', 'cloudid', 'deviceid'}
EXCLUDED_MESSAGES = {1000, 1001, 1412, 1422, 1426, 1432, 1561}
MAX_BODY = 256*1024
PRIVATE_KEY_PARTS=re.compile('password|passwd|passphrase|secret|token|credential|sessionid|serialnumber|serialnum|cloudid|deviceid|authkey|communicatekey|encryptkey')


class PrivateHandler(RotatingFileHandler):
    def _open(self):
        fd=os.open(self.baseFilename,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        return os.fdopen(fd,'a',encoding='utf-8')

    def handleError(self, record):
        # Never send a failed log record (or its body) to stderr.
        raise OSError('Recorder journal write failed')


class EventJournal:
    def __init__(self,path,config,*,max_bytes=5*1024*1024,backups=3,queue_bytes=2*1024*1024):
        secrets={str(config[key]) for key in ('SERIAL_NUM','USERNAME','PASSWORD') if config.get(key)}
        if config.get('PASSWORD'):
            digest=hashlib.md5(config['PASSWORD'].encode()).digest()
            alphabet='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            secrets.add(''.join(alphabet[(digest[i]+digest[i+1])%62] for i in range(0,16,2)))
            secrets.add(digest.hex())
        self.secrets=re.compile('|'.join(re.escape(value) for value in sorted(secrets,key=len,reverse=True))) if secrets else None
        self.handler=PrivateHandler(path,maxBytes=max_bytes,backupCount=backups,delay=True,encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.limit=queue_bytes
        self.condition=threading.Condition()
        self.items=deque()
        self.size=self.dropped=self.written=self.write_failures=0
        self.closed=False
        self.thread=threading.Thread(target=self.run,name='recorder-journal',daemon=True)
        self.thread.start()

    def record(self,fields,body,channel=None):
        mid=fields[-2]
        if mid in EXCLUDED_MESSAGES:return
        metadata=dict(received_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            message_id=mid,stream_channel=channel,body_bytes=len(body))
        if len(body)>MAX_BODY:
            metadata['body_omitted']='size_limit'
            body=b''
        cost=len(body)+256
        with self.condition:
            if self.closed:return
            if len(self.items)>=256 or self.size+cost>self.limit:
                self.dropped+=1
                return
            self.items.append((metadata,body,cost))
            self.size+=cost
            self.condition.notify()

    def redact(self,value):
        if isinstance(value,dict):
            result={}
            for key,item in value.items():
                normalized=re.sub('[^a-z0-9]','',key.lower())
                safe_key=self.secrets.sub('[redacted]',key) if self.secrets else key
                result[safe_key]='[redacted]' if normalized in PRIVATE_KEYS or PRIVATE_KEY_PARTS.search(normalized) else self.redact(item)
            return result
        if isinstance(value,list):return [self.redact(item) for item in value]
        if isinstance(value,str):
            if value.lstrip().startswith(('{','[')):
                try:value=json.dumps(self.redact(json.loads(value)),ensure_ascii=False)
                except (ValueError,UnicodeError):pass
            if self.secrets:return self.secrets.sub('[redacted]',value)
        return value

    def encode(self,metadata,body):
        if 'body_omitted' not in metadata:
            try:
                metadata['body']=self.redact(json.loads(body.rstrip(b'\n\0')))
            except (ValueError,UnicodeError,RecursionError):
                # Unknown binary/text can contain credentials or media. Keep
                # its identity/size, without writing unsafe raw bytes.
                metadata['body_omitted']='non_json'
        line=json.dumps(metadata,ensure_ascii=False,separators=(',',':'))
        encoded=line.encode('utf-8',errors='backslashreplace')
        if len(encoded)>2*MAX_BODY:
            metadata.pop('body',None)
            metadata['body_omitted']='encoded_size_limit'
            line=json.dumps(metadata,separators=(',',':'))
            return line
        return encoded.decode('utf-8')

    def stats(self):
        with self.condition:
            return dict(written=self.written,dropped=self.dropped,write_failures=self.write_failures,queued_bytes=self.size)

    def run(self):
        previous=None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda:self.items or self.closed)
                    if not self.items:return
                    batch=[self.items.popleft() for _ in range(min(8,len(self.items)))]
                    self.size-=sum(cost for _,_,cost in batch)
                    status=(self.dropped,self.write_failures)
                lines=[self.encode(meta,body) for meta,body,_ in batch]
                if status!=previous and any(status):
                    lines.append(json.dumps(dict(type='journal_status',dropped=status[0],write_failures=status[1])))
                try:
                    self.handler.emit(logging.LogRecord('recorder-events',logging.INFO,'',0,'\n'.join(lines),(),None))
                except Exception:
                    with self.condition:
                        self.dropped+=len(batch);self.write_failures+=1
                        self.condition.notify_all()
                        self.condition.wait_for(lambda:self.closed,timeout=1)
                    continue
                with self.condition:
                    self.written+=len(batch)
                    self.condition.notify_all()
                previous=status
        finally:self.handler.close()

    def close(self):
        with self.condition:
            self.closed=True
            self.condition.notify_all()
        self.thread.join(timeout=2)
