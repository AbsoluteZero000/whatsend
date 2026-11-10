"""Read MP4 movie duration without loading media into memory or spawning a process."""
import math
from pathlib import Path
import struct


def video_duration(path: str) -> float:
    file_size = Path(path).stat().st_size
    if not 0 < file_size <= 50 * 1024 * 1024 or Path(path).suffix.lower() != '.mp4':
        raise ValueError("Choose an MP4 video of at most 50 MB")
    with open(path, 'rb') as stream:
        def boxes(start, end):
            offset = start
            for _ in range(10000):
                if offset == end:
                    return
                if end - offset < 8:
                    raise ValueError("Invalid MP4 container")
                stream.seek(offset)
                size, kind = struct.unpack('>I4s', stream.read(8))
                header_size = 8
                if size == 1:
                    if end - offset < 16:
                        raise ValueError("Invalid MP4 container")
                    size = struct.unpack('>Q', stream.read(8))[0]
                    header_size = 16
                elif size == 0:
                    size = end - offset
                if size < header_size or offset + size > end:
                    raise ValueError("Invalid MP4 container")
                yield kind, offset + header_size, offset + size
                offset += size
            raise ValueError("MP4 container has too many boxes")

        for kind, start, end in boxes(0, file_size):
            if kind != b'moov':
                continue
            for child, payload, child_end in boxes(start, end):
                if child != b'mvhd':
                    continue
                stream.seek(payload)
                data = stream.read(min(32, child_end - payload))
                if len(data) >= 20 and data[0] == 0:
                    scale, duration = struct.unpack('>II', data[12:20])
                elif len(data) >= 32 and data[0] == 1:
                    scale, duration = struct.unpack('>IQ', data[20:32])
                else:
                    raise ValueError("Invalid MP4 movie header")
                seconds = duration / scale if scale else 0
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("The MP4 has no valid duration")
                return seconds
    raise ValueError("The MP4 has no movie duration; re-export it before uploading")
