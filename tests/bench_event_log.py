"""CPU-only journal encoding/skip benchmark; no disk, SDK or network."""
import json
import timeit
from backend.cameras.event_log import EventJournal


if __name__=='__main__':
    journal=EventJournal.__new__(EventJournal)
    journal.secrets=None
    for count in (1,16,64):
        body=json.dumps(dict(AlarmInfo=dict(Targets=[dict(Rect=[1,2,3,4],Type='Human') for _ in range(count)]))).encode()
        elapsed=timeit.timeit(lambda:journal.encode(dict(message_id=1504),body),number=1000)
        print(dict(targets=count,body_bytes=len(body),encode_us=round(elapsed*1000,2)))
    elapsed=timeit.timeit(lambda:journal.record((0,1412,0),b'video'),number=100000)
    print(dict(media_skip_us=round(elapsed*10,3)))
