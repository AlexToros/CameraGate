"""Manual in-memory fan-out benchmark: python -m tests.backend.bench_relay."""
from time import perf_counter
from .test_relay import initialized
from backend.cameras.relay import pack_encoded, SessionHealth


def main():
    for count in (1,16,64,256):
        health=SessionHealth()
        messages=[dict(type='media',cid=cid) for cid in range(count)]
        started=perf_counter()
        for _ in range(1000):
            for meta in messages:
                health.observe(meta)
                _expired=health.expired
        print(f'session_health cameras={count} packets={count*1000} ms={(perf_counter()-started)*1000:.2f}')
    for count in (1,16,32,64):
        for history in (5,50,150):
            sources={cid:initialized(cid) for cid in range(1,count+1)}
            for source in sources.values():
                for _ in range(history):
                    source.feed(dict(type='media',generation='first',key=False),b'x'*128)
            sent={}
            started=perf_counter()
            bootstrap=len(pack_encoded(sources,sent)[1])
            for _ in range(200):
                for source in sources.values():
                    source.feed(dict(type='media',generation='first',key=False),b'x'*128)
                pack_encoded(sources,sent)
            print(f'cameras={count} cached_samples={history} bootstrap_bytes={bootstrap} incremental_200_ms={(perf_counter()-started)*1000:.2f}')


if __name__=='__main__':
    main()
