"""Manual in-memory fan-out benchmark: python -m tests.backend.bench_relay."""
from time import perf_counter
from .test_relay import initialized
from backend.cameras.relay import pack_encoded


def main():
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
