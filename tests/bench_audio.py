"""Run from the repository root: python -m tests.bench_audio (no camera I/O)."""
import struct
import timeit
from backend.cameras.vesta import Frames
from backend.cameras.audio import AudioClock


def run(count):
    frame=b'\0\0\1\xfa\x0e\x02'+struct.pack('<H',320)+b'\xd5'*320
    data=frame*count
    def batch():
        chunks=[];Frames().feed_units(data,chunks);AudioClock().batch(chunks,count*.04)
    return timeit.timeit(batch,number=500)/500


if __name__=='__main__':
    small=run(16);medium=run(64);large=run(256)
    print(dict(frames16_us=round(small*1e6),frames64_us=round(medium*1e6),frames256_us=round(large*1e6)))
