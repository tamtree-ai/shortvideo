"""Real image headers, built to order.

`images.py` reads dimensions out of PNG, JPEG and WEBP headers, and the only
way to test that honestly is to hand it headers whose dimensions are known by
construction. Each builder writes the real structure — IHDR, a JPEG SOF
segment, the three WEBP variants — so a parser that reads the wrong offset
fails rather than agreeing with a mock.
"""

from __future__ import annotations

import struct
import zlib


def png_bytes(*, width: int = 1080, height: int = 1920) -> bytes:
    """A PNG with a real IHDR. The pixel data is not needed to read a size."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def jpeg_bytes(*, width: int = 1080, height: int = 1920, padding_segments: int = 2) -> bytes:
    """A JPEG whose SOF0 sits behind `padding_segments` other segments.

    The padding is the point: a JPEG's size is not at a fixed offset, and a
    parser that guessed one would pass against a minimal file and fail against
    every real photo, which carries EXIF and quantisation tables first.
    """
    stream = b"\xff\xd8"
    for index in range(padding_segments):
        payload = b"\x00" * (8 + index)
        stream += b"\xff\xe0" + struct.pack(">H", len(payload) + 2) + payload
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    stream += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    return stream + b"\xff\xd9"


def _riff(chunk: bytes, payload: bytes) -> bytes:
    body = chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def webp_vp8x_bytes(*, width: int = 1080, height: int = 1920) -> bytes:
    """Extended WEBP: the canvas size, stored minus one, three bytes each."""
    payload = (
        b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    return _riff(b"VP8X", payload)


def webp_vp8l_bytes(*, width: int = 1080, height: int = 1920) -> bytes:
    """Lossless WEBP: fourteen bits each, packed after the signature byte."""
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8
    return _riff(b"VP8L", payload)


def webp_vp8_bytes(*, width: int = 1080, height: int = 1920) -> bytes:
    """Lossy WEBP: fourteen bits each, after the three-byte start code."""
    payload = (
        b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\x00" * 8
    )
    return _riff(b"VP8 ", payload)


def heic_bytes(*, brand: bytes = b"heic") -> bytes:
    """Enough ISO-BMFF to be recognised. Dimensions are deliberately not
    readable — `images.py` says why it does not walk these boxes."""
    return struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x00\x00" + brand + b"\x00" * 8
