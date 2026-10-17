"""Compressed DVRIP access units → ISO BMFF. No video decoding or encoding.

FFmpeg creates the initialization metadata once per codec configuration. Live
samples are packed directly in Python; no FFmpeg process remains per camera.
DVRIP supplies frame cadence, and its surveillance streams use decode order
without B frames. Reordered streams must not be silently treated as this format.
"""
import os
import re
import shutil
import struct
import subprocess

from ..paths import ROOT

MAX_SAMPLE = 8 * 1024 * 1024
START = re.compile(b'\x00\x00(?:\x00)?\x01')


class ReorderedStream(ValueError):
    pass


def box(kind, body):
    return struct.pack('!I4s', len(body) + 8, kind) + body


def full_box(kind, version, flags, body):
    return box(kind, bytes([version]) + flags.to_bytes(3, 'big') + body)


def boxes(data):
    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated MP4 box')
        size, kind = struct.unpack_from('!I4s', data, offset)
        if size < 8 or offset + size > len(data):
            raise ValueError('Invalid MP4 box size')
        yield kind, data[offset:offset+size]
        offset += size


def parameter_sets(data, codec):
    types = {32, 33, 34} if codec == 3 else {7, 8}
    return tuple(nal for nal in START.split(data) if nal and
                 ((nal[0] >> 1) & 63 if codec == 3 else nal[0] & 31) in types)


def codec_string(init, codec):
    marker = b'hvcC' if codec == 3 else b'avcC'
    at = init.find(marker)
    if at < 4:
        raise ValueError('Missing codec configuration')
    size = struct.unpack_from('!I', init, at-4)[0]
    data = init[at+4:at-4+size]
    if codec == 2 and len(data) >= 4:
        return 'avc1.' + data[1:4].hex().upper()
    if codec != 3 or len(data) < 23:
        raise ValueError('Invalid codec configuration')
    space = ('', 'A', 'B', 'C')[data[1] >> 6]
    profile = data[1] & 31
    compatibility = int('{:032b}'.format(int.from_bytes(data[2:6], 'big'))[::-1], 2)
    constraints = data[6:12].rstrip(b'\0')
    suffix = ''.join('.{:02X}'.format(value) for value in constraints)
    return 'hvc1.{}{}.{:X}.{}{}{}'.format(space, profile, compatibility,
        'H' if data[1] & 32 else 'L', data[12], suffix)


def initialization(video, codec, fps):
    binary = os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
    if not binary:
        binary = str(next((ROOT/'.runtime/packages/imageio_ffmpeg/binaries').glob('ffmpeg-*')))
    command = [binary, '-hide_banner', '-loglevel', 'info', '-threads', '1',
        '-r', str(fps), '-f', 'hevc' if codec == 3 else 'h264', '-i', 'pipe:0',
        '-map', '0:v:0', '-an', '-c:v', 'copy', '-bsf:v', 'trace_headers', '-frames:v', '1',
        '-tag:v', 'hvc1' if codec == 3 else 'avc1', '-video_track_timescale', '90000',
        '-movflags', 'empty_moov+default_base_moof+frag_keyframe', '-f', 'mp4', 'pipe:1']
    result = subprocess.run(command, input=video, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=8, check=True)
    # Raw DVRIP access units have no per-picture PTS. Reject streams advertising
    # picture reordering rather than silently assigning incorrect timestamps.
    reorder = re.findall(rb'(?:sps_max_num_reorder_pics\[\d+\]|max_num_reorder_frames)\s+[^\r\n]*=\s*(\d+)', result.stderr)
    if any(int(value)>0 for value in reorder):
        raise ReorderedStream('Picture reordering requires presentation timestamps')
    init = b''.join(value for kind, value in boxes(result.stdout) if kind in (b'ftyp', b'moov'))
    if not init or len(init) > 65536:
        raise ValueError('Invalid initialization segment')
    return init


class Muxer:
    def __init__(self, video, codec, fps):
        if codec not in (2, 3) or not 1 <= fps <= 60:
            raise ValueError('Unsupported stream')
        self.codec, self.fps = codec, fps
        self.parameters = parameter_sets(video, codec)
        if not self.parameters:
            raise ValueError('Missing parameter sets')
        self.init = initialization(video, codec, fps)
        self.mime = 'video/mp4; codecs="{}"'.format(codec_string(self.init, codec))
        self.sequence, self.timestamp = 0, 0

    def fragment(self, video, key):
        if len(video) > MAX_SAMPLE:
            raise ValueError('Oversized video sample')
        nals = START.split(video)
        excluded = {32, 33, 34, 35} if self.codec == 3 else {7, 8, 9}
        sample = b''.join(struct.pack('!I', len(nal)) + nal for nal in nals if nal and
            ((nal[0] >> 1) & 63 if self.codec == 3 else nal[0] & 31) not in excluded)
        if not sample:
            raise ValueError('Empty access unit')
        self.sequence += 1
        end = round(self.sequence * 90000 / self.fps)
        duration = end - self.timestamp
        tfhd = full_box(b'tfhd', 0, 0x020000, struct.pack('!I', 1))
        tfdt = full_box(b'tfdt', 1, 0, struct.pack('!Q', self.timestamp))
        mfhd = full_box(b'mfhd', 0, 0, struct.pack('!I', self.sequence))
        def moof(offset):
            trun = full_box(b'trun', 0, 0x000701, struct.pack('!IIIII',
                1, offset, duration, len(sample), 0x02000000 if key else 0x01010000))
            return box(b'moof', mfhd + box(b'traf', tfhd + tfdt + trun))
        header = moof(0)
        header = moof(len(header) + 8)
        self.timestamp = end
        return header + box(b'mdat', sample)
