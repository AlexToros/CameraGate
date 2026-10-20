"""Bounded G.711 A-law passthrough; decoding stays in the selected browser."""
import struct

HEADER = struct.Struct('!BBd')
RATE = 8000
MAX_AUDIO = 16000


class AudioClock:
    def __init__(self):
        self.next_time = None

    def batch(self, chunks, video_time):
        data = b''.join(chunks)[-RATE:]
        if not data:
            return b''
        duration = len(data)/RATE
        if self.next_time is None or abs(self.next_time+duration-video_time) > .5:
            self.next_time = max(0, video_time-duration)
        start = self.next_time
        self.next_time += duration
        return HEADER.pack(14, 2, start)+data
