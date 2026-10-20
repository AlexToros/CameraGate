"""In-memory channel reconciliation; no native SDK, network or database."""
import threading
import timeit
from types import SimpleNamespace

from backend.cameras.relay_worker import reconcile_channels


def run(count):
    channels={}
    selections=({cid:'a' for cid in range(count)},{cid:'b' for cid in range(count)})
    def create(cid,token):
        return SimpleNamespace(token=token,stop=threading.Event(),
            thread=SimpleNamespace(is_alive=lambda:False))
    def change():
        for selected in selections:
            reconcile_channels(channels,selected,create)
    return timeit.timeit(change,number=500)/1000


if __name__=='__main__':
    print({f'channels{count}_us':round(run(count)*1e6,2) for count in (16,64,256)})
