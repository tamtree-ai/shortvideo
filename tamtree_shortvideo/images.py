"""Get a workspace attachment into a MiniMax request, or say why it cannot go.

**The problem V2.5 exists to solve.** MiniMax takes an image as a public URL,
an `mm_file://` reference or a base64 data URI. A Tamtree `BinaryRef` is none
of those: it names bytes in the workspace's object store, reachable by the
worker and by nobody else. So something has to carry them across, and the plan
left the choice open between a bounded data URI and a vendor upload step.

**v1 sends data URIs.** MiniMax caps one image at 30 MB and the whole request
body at 64 MB, so a first/last frame pair fits with room to spare even after
base64's 4/3 inflation — and a data URI costs no second round trip, creates no
vendor-side file to expire or leak, and adds no upload call with its own
failure and idempotency surface. A vendor upload earns its complexity when
something needs to exceed those limits; the escape hatch until then is a public
URL, which this node also accepts.

**Every check here runs before the request, and the format is read from the
bytes.** A `BinaryRef.mime_type` is whatever some upstream step wrote down —
an HTTP header, a filename, a guess. The magic bytes are what MiniMax's decoder
will actually see, so those are what the format is taken from, and a mismatch
is the user's `.png` that is really a JPEG rather than a failed run.

**HEIC and HEIF are accepted but not measured.** Reading their dimensions means
walking ISO-BMFF boxes, which is a real parser for a format nobody is feeding a
vertical short from. They pass the format and size checks and MiniMax judges
their dimensions — which costs a run if they are wrong, but never a generation,
because a rejected create bills nothing.
"""

from __future__ import annotations

import base64
import struct
from typing import Final

from tamtree_plugin_sdk import BinaryRef, NodeConfigurationError

__all__ = [
    "MAX_ASPECT_RATIO",
    "MAX_IMAGE_BYTES",
    "MAX_PIXELS",
    "MAX_REQUEST_BYTES",
    "MIN_ASPECT_RATIO",
    "MIN_PIXELS",
    "SUPPORTED_FORMATS",
    "data_uri",
    "dimensions",
    "image_format",
    "validate_image",
]

#: MiniMax's own limits, checked against the live create reference 2026-09-19.
MAX_IMAGE_BYTES: Final = 30 * 1024 * 1024
MAX_REQUEST_BYTES: Final = 64 * 1024 * 1024
MIN_PIXELS: Final = 256
MAX_PIXELS: Final = 5_760
MIN_ASPECT_RATIO: Final = 0.4
MAX_ASPECT_RATIO: Final = 2.5

#: The formats the create contract lists. The value is what goes in the data
#: URI's media type, which MiniMax requires in lower case — `jpg` and `jpeg`
#: are the same bytes, and `image/jpeg` is the only spelling of it.
SUPPORTED_FORMATS: Final = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}

#: ISO-BMFF brands that mean "this is HEIF-family". `mif1`/`msf1` are the
#: generic still/sequence brands; the `he*` ones are HEVC-coded.
_HEIF_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
)


def image_format(data: bytes) -> str | None:
    """The format the decoder will see, from the magic bytes. None if unknown.

    Read from the content rather than from `BinaryRef.mime_type`, which is
    whatever an upstream step happened to write down.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "heif" if data[8:12] in (b"mif1", b"msf1") else "heic"
    return None


def dimensions(data: bytes, fmt: str) -> tuple[int, int] | None:
    """`(width, height)`, or None when the format is not measured here.

    Only the header is read — never the pixels — so this stays cheap on a
    30 MB file.
    """
    if fmt == "png":
        return _png_dimensions(data)
    if fmt == "jpeg":
        return _jpeg_dimensions(data)
    if fmt == "webp":
        return _webp_dimensions(data)
    return None  # heic/heif — see the module docstring


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """IHDR is required to be the first chunk, at a fixed offset."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return int(width), int(height)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the segments to the start-of-frame, which is where the size lives.

    The size is not at a fixed offset in a JPEG: it follows however many
    application, quantisation and Huffman segments the encoder wrote first.
    """
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        # Standalone markers carry no length field.
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # end of image / start of scan
            return None
        (length,) = struct.unpack_from(">H", data, offset + 2)
        # SOF0..SOF15, minus the three markers that share the range but are
        # not frame headers: DHT (C4), JPG (C8) and DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack_from(">HH", data, offset + 5)
            return int(width), int(height)
        if length < 2:
            return None
        offset += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Three sub-formats, three different places the size is written."""
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    return None


def validate_image(data: bytes, *, label: str) -> str:
    """Refuse anything MiniMax would, and return the detected format.

    `label` names the role in the message — "first frame", "reference image 2"
    — because "the image is too small" is not actionable on a request carrying
    four of them.
    """
    if not data:
        raise NodeConfigurationError(
            f"The {label} attachment is empty, so there is no image to send."
        )
    fmt = image_format(data)
    if fmt is None:
        raise NodeConfigurationError(
            f"The {label} is not an image MiniMax accepts — its content does not look like "
            f"any of {', '.join(sorted(SUPPORTED_FORMATS))}. Note that the format is read from "
            "the bytes, not from the attachment's declared type, so a file named `.png` that "
            "is really something else is caught here."
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise NodeConfigurationError(
            f"The {label} is {len(data) / 1024 / 1024:.1f} MB and MiniMax accepts at most "
            f"{MAX_IMAGE_BYTES // 1024 // 1024} MB per image. Resize it, or host it and pass "
            "the https URL instead of the attachment."
        )

    size = dimensions(data, fmt)
    if size is None:
        return fmt  # heic/heif, or a header this module could not read
    width, height = size
    if not (MIN_PIXELS <= width <= MAX_PIXELS and MIN_PIXELS <= height <= MAX_PIXELS):
        raise NodeConfigurationError(
            f"The {label} is {width}×{height} and MiniMax accepts {MIN_PIXELS}–{MAX_PIXELS} "
            "pixels on each side."
        )
    ratio = width / height
    if not MIN_ASPECT_RATIO <= ratio <= MAX_ASPECT_RATIO:
        raise NodeConfigurationError(
            f"The {label} is {width}×{height}, an aspect ratio of {ratio:.2f}, and MiniMax "
            f"accepts {MIN_ASPECT_RATIO} to {MAX_ASPECT_RATIO}. A 9:16 frame is 0.56, which "
            "is inside it — this image is far narrower or wider than any video frame."
        )
    return fmt


def data_uri(data: bytes, fmt: str) -> str:
    """`data:image/png;base64,…` — the media type lower case, as required."""
    return f"data:{SUPPORTED_FORMATS[fmt]};base64,{base64.b64encode(data).decode('ascii')}"


def describe(ref: BinaryRef) -> str:
    """A short, safe description of an attachment for an error message."""
    return ref.file_name or ref.mime_type or ref.id
