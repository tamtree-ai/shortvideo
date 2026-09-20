"""Synthetic audio whose true duration is known by construction.

`audio_duration` is measured against containers built here rather than against
a checked-in sample, because a sample proves the parser agrees with one file
while a builder proves it agrees with arithmetic: the tests below can say "this
is exactly 0.48 seconds" and mean it, and can vary the sample rate, the frame
count and the pre-skip to see the measurement follow.

The three builders emit the three containers Google actually returns for the
encodings the node offers — a WAV-wrapped `LINEAR16`, an MPEG-2 Layer III
`MP3`, and an Ogg-wrapped `OGG_OPUS`. `pcm_bytes` is the fourth, headerless
shape: what OpenRouter's `/audio/speech` returns for `openrouter_tts`.
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Final

# -- MP3 ---------------------------------------------------------------------
# MPEG-2 Layer III, 32kbps, 24kHz mono: the shape Google's documented "MP3
# audio at 32kbps" takes at the default sample rate of most voices.

MP3_SAMPLE_RATE: Final = 24_000
MP3_SAMPLES_PER_FRAME: Final = 576
#: (576/8) * 32000 // 24000 — the Layer III frame-length formula, evaluated.
MP3_FRAME_BYTES: Final = 96
MP3_FRAME_SECONDS: Final = MP3_SAMPLES_PER_FRAME / MP3_SAMPLE_RATE

OPUS_GRANULE_RATE: Final = 48_000
OPUS_PRE_SKIP: Final = 312


def pcm_bytes(*, seconds: float = 0.5, sample_rate: int = 24_000) -> bytes:
    """Silent 16-bit mono PCM of exactly `seconds`, no container at all."""
    return b"\x00\x00" * round(sample_rate * seconds)


def wav_bytes(*, seconds: float = 0.5, sample_rate: int = 24_000, channels: int = 1) -> bytes:
    """A silent PCM WAV of exactly `seconds`, header and all."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * seconds) * channels)
    return buffer.getvalue()


def mp3_frame_header(*, padding: int = 0) -> bytes:
    """The 32-bit header the constants above describe, field by field."""
    header = (
        0x7FF << 21  # frame sync
        | 0b10 << 19  # MPEG-2
        | 0b01 << 17  # Layer III
        | 1 << 16  # no CRC
        | 4 << 12  # bitrate index 4 = 32kbps for MPEG-2 Layer III
        | 1 << 10  # sample-rate index 1 = 24000 Hz for MPEG-2
        | padding << 9
        | 0b11 << 6  # single channel
    )
    return header.to_bytes(4, "big")


def mp3_bytes(*, frames: int = 20, id3: bool = False, xing: bool = False) -> bytes:
    """`frames` identical silent frames, optionally behind the tags real
    encoders put there."""
    frame = mp3_frame_header() + b"\x00" * (MP3_FRAME_BYTES - 4)
    stream = frame * frames
    if xing:
        # A Xing frame is a real frame carrying the VBR table and no audio.
        stream = mp3_frame_header() + b"\x00" * 32 + b"Xing" + b"\x00" * (MP3_FRAME_BYTES - 40)
        stream += frame * frames
    if id3:
        size = 64
        syncsafe = bytes(((size >> shift) & 0x7F) for shift in (21, 14, 7, 0))
        stream = b"ID3" + bytes([4, 0, 0]) + syncsafe + b"\x00" * size + stream
    return stream


def ogg_page(body: bytes, *, granule: int, flags: int, seq: int, serial: int = 1) -> bytes:
    """One Ogg page: the 27-byte header, its segment table, then the body."""
    segments = [255] * (len(body) // 255) + [len(body) % 255]
    return (
        b"OggS"
        + bytes([0, flags])
        + struct.pack("<q", granule)
        + struct.pack("<I", serial)
        + struct.pack("<I", seq)
        + struct.pack("<I", 0)  # CRC: never checked, so never computed
        + bytes([len(segments)])
        + bytes(segments)
        + body
    )


def ogg_opus_bytes(*, samples: int, pre_skip: int = OPUS_PRE_SKIP) -> bytes:
    """An Opus stream that plays for exactly `samples / 48000` seconds."""
    opus_head = (
        b"OpusHead"
        + bytes([1, 1])  # version, channel count
        + struct.pack("<H", pre_skip)
        + struct.pack("<I", 48_000)  # original input rate
        + struct.pack("<h", 0)  # output gain
        + bytes([0])  # mapping family
    )
    return ogg_page(opus_head, granule=0, flags=0x02, seq=0) + ogg_page(
        b"\x00" * 40, granule=pre_skip + samples, flags=0x04, seq=1
    )
