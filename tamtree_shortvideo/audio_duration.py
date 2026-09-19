"""Measure how long the synthesized audio actually plays, from the bytes.

**Why this module exists at all.** `text:synthesize` returns audio and mark
timepoints; it does **not** return a duration. But narration duration is what
the whole pipeline is built on — §3's "audio drives the timeline, not the
video" is the reason a beat is split, a clip is trimmed and a caption lands
where it does. A duration that is inferred, estimated from a character count or
copied from a request parameter would be wrong in exactly the way that is
invisible until the finished short is out of sync.

So every number here is **derived from the container**, and an encoding whose
container cannot be measured exactly is refused rather than guessed at. Three
forms cover everything `shortvideo.google_tts` offers:

- **WAV-wrapped** (`LINEAR16`) — the `fmt ` chunk's byte rate against the
  `data` chunk's length. Google documents that LINEAR16 "also contains a WAV
  header"; `PCM` is the same samples *without* one, which is why the node does
  not offer it.
- **MP3** — every frame header walked and its own duration summed, so a
  variable-bitrate stream is as exact as a constant one. Google documents MP3
  output as 32kbps, which at its default sample rate is MPEG-2 Layer III, so
  the MPEG-2 tables are not hypothetical.
- **OGG_OPUS** — the last page's granule position minus the pre-skip from
  `OpusHead`, over the 48kHz Opus always counts in.

**Why the errors are `NodeConfigurationError`.** Same reasoning as the token
mint: the engine only marks a step non-retryable, and carries its sentence to
the run banner, for that class. Bytes that could not be parsed on the first
attempt parse no better on the second — the call has already been paid for and
a retry would pay again for the same unreadable answer.
"""

from __future__ import annotations

import struct
from typing import Final

from tamtree_plugin_sdk import NodeConfigurationError

__all__ = ["AudioDurationError", "MEASURABLE_ENCODINGS", "duration_seconds"]


class AudioDurationError(NodeConfigurationError):
    """The returned audio could not be measured exactly.

    Non-retryable: the same bytes will not parse differently next time, and the
    synthesis has already been billed.
    """


#: The encodings this module measures exactly, and therefore the only ones the
#: node offers. `PCM` is headerless, `M4A` needs an MP4 box parser, and
#: `MULAW`/`ALAW` are telephony codecs with nothing to offer a vertical short.
MEASURABLE_ENCODINGS: Final = ("LINEAR16", "MP3", "OGG_OPUS")

#: Opus granule positions are counted at 48kHz whatever the input rate was.
_OPUS_GRANULE_RATE: Final = 48_000

# -- MPEG audio frame tables -------------------------------------------------
# Indexed exactly as the two bit-fields in the frame header are, so a reserved
# or "free format" slot stays `None` and is refused rather than silently read
# as some neighbouring value.

_MPEG1: Final = "1"
_MPEG2: Final = "2"
_MPEG25: Final = "2.5"

_VERSION_BY_BITS: Final = {0b00: _MPEG25, 0b01: None, 0b10: _MPEG2, 0b11: _MPEG1}

_SAMPLE_RATES: Final = {
    _MPEG1: (44_100, 48_000, 32_000, None),
    _MPEG2: (22_050, 24_000, 16_000, None),
    _MPEG25: (11_025, 12_000, 8_000, None),
}

#: Layer III only — the one layer Google emits. Entry 0 is "free format" (the
#: bitrate is not in the header at all) and entry 15 is invalid; both are
#: `None`, so the walk stops with a named error instead of dividing by nothing.
_LAYER3_BITRATES_KBPS: Final = {
    _MPEG1: (
        None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None,
    ),
    _MPEG2: (
        None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None,
    ),
}  # fmt: skip

_SAMPLES_PER_FRAME: Final = {_MPEG1: 1152, _MPEG2: 576, _MPEG25: 576}

#: How much unparseable tail is accepted after the last good frame before the
#: measurement is called into question. An ID3v1 tag is exactly 128 bytes; this
#: leaves room for one and little else, so a genuinely truncated stream is
#: still caught.
_MAX_TRAILING_BYTES: Final = 256

_TRAILING_TAGS: Final = (b"TAG", b"ID3", b"APETAGEX", b"LYRICSBEGIN")


def duration_seconds(data: bytes, *, encoding: str) -> float:
    """The exact playing time of `data`, in seconds.

    `encoding` is the Google `AudioEncoding` name the audio was requested as.
    """
    if not data:
        raise AudioDurationError(
            "Google returned an empty audio body, so there is nothing to measure. "
            "Re-running will not help; check the voice and input in this step."
        )
    if encoding == "LINEAR16":
        return _wav_duration(data)
    if encoding == "MP3":
        return _mp3_duration(data)
    if encoding == "OGG_OPUS":
        return _ogg_opus_duration(data)
    raise AudioDurationError(
        f"Cannot measure the duration of {encoding!r} audio exactly, and the timeline is built "
        f"on that measurement. Choose one of {', '.join(MEASURABLE_ENCODINGS)}."
    )


def _wav_duration(data: bytes) -> float:
    """Byte rate from `fmt `, payload length from `data`.

    The declared `data` length is preferred but not trusted: a streamed WAV can
    carry `0` or `0xFFFFFFFF` there, and the bytes actually in hand are the
    better answer whenever the declaration exceeds them.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioDurationError(
            "Google returned LINEAR16 audio without the WAV header it documents, so its "
            "duration cannot be measured. Report this rather than retrying."
        )

    byte_rate = 0
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (declared,) = struct.unpack_from("<I", data, offset + 4)
        body = offset + 8

        if chunk_id == b"fmt " and declared >= 16 and body + 16 <= len(data):
            _format, channels, sample_rate, declared_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
            byte_rate = declared_rate or (sample_rate * channels * bits // 8)
        elif chunk_id == b"data":
            available = len(data) - body
            payload = declared if 0 < declared <= available else available
            if byte_rate <= 0:
                raise AudioDurationError(
                    "The returned WAV names no usable byte rate, so its duration cannot be "
                    "measured. Report this rather than retrying."
                )
            return payload / byte_rate

        offset = body + declared + (declared % 2)  # chunks are word-aligned

    raise AudioDurationError(
        "The returned WAV has no `data` chunk, so it carries no audio to measure. "
        "Report this rather than retrying."
    )


def _mp3_duration(data: bytes) -> float:
    """Walk every frame and sum the time each one represents.

    Summing per frame rather than dividing the file size by a nominal bitrate
    is what makes this exact for a variable-bitrate stream, and it costs one
    pass over a few hundred kilobytes.
    """
    offset = _skip_id3v2(data)
    total = 0.0
    frames = 0

    while offset + 4 <= len(data):
        frame = _parse_frame_header(data, offset)
        if frame is None:
            break
        length, seconds = frame
        if offset + length > len(data):
            break  # a truncated final frame contributes no complete audio
        if frames == 0 and _is_vbr_header_frame(data, offset, length):
            # Xing/Info: a real frame that carries the VBR table and silence.
            # Counting it would add its ~26ms to every measurement.
            offset += length
            continue
        total += seconds
        frames += 1
        offset += length

    if frames == 0:
        raise AudioDurationError(
            "The returned MP3 contains no readable MPEG audio frame, so its duration cannot "
            "be measured. Report this rather than retrying."
        )

    trailing = len(data) - offset
    if trailing > _MAX_TRAILING_BYTES and not data[offset : offset + 16].startswith(_TRAILING_TAGS):
        raise AudioDurationError(
            f"The returned MP3 stops being readable {trailing} bytes before its end, so the "
            "measured duration would be short and the timeline would drift. The download is "
            "likely truncated; report this rather than retrying."
        )
    return total


def _skip_id3v2(data: bytes) -> int:
    """Past a leading ID3v2 tag, whose size is stored seven bits per byte."""
    if len(data) < 10 or data[0:3] != b"ID3":
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return min(10 + size, len(data))


def _parse_frame_header(data: bytes, offset: int) -> tuple[int, float] | None:
    """`(frame length in bytes, frame duration in seconds)`, or None if this is
    not a valid MPEG Layer III frame header."""
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset : offset + 4], "big")
    if header >> 21 != 0x7FF:  # the eleven-bit frame sync
        return None

    version = _VERSION_BY_BITS[(header >> 19) & 0b11]
    layer_bits = (header >> 17) & 0b11
    if version is None or layer_bits != 0b01:  # 0b01 is Layer III
        return None

    bitrate_table = _LAYER3_BITRATES_KBPS[_MPEG1 if version == _MPEG1 else _MPEG2]
    bitrate_kbps = bitrate_table[(header >> 12) & 0b1111]
    sample_rate = _SAMPLE_RATES[version][(header >> 10) & 0b11]
    if not bitrate_kbps or not sample_rate:
        return None

    padding = (header >> 9) & 0b1
    samples = _SAMPLES_PER_FRAME[version]
    # Layer III packs `samples` into `samples / 8` bytes at one bit per bit/s:
    # 144 bytes per kbps at 1152 samples, 72 at 576.
    length = (samples // 8) * bitrate_kbps * 1000 // sample_rate + padding
    if length <= 4:
        return None
    return length, samples / sample_rate


def _is_vbr_header_frame(data: bytes, offset: int, length: int) -> bool:
    """A Xing or Info tag inside the frame's side-information area."""
    body = data[offset : offset + length]
    return b"Xing" in body[:64] or b"Info" in body[:64]


def _ogg_opus_duration(data: bytes) -> float:
    """The last page's granule position, less the encoder's pre-skip.

    Opus counts granules at 48kHz whatever it was handed, and `OpusHead`'s
    pre-skip is the priming samples that are decoded but not played — dropping
    it is the difference between the file's length and what a listener hears.
    """
    pre_skip: int | None = None
    final_granule: int | None = None
    offset = 0

    while offset + 27 <= len(data):
        if data[offset : offset + 4] != b"OggS":
            raise AudioDurationError(
                "The returned OGG_OPUS audio is not a readable Ogg stream, so its duration "
                "cannot be measured. Report this rather than retrying."
            )
        (granule,) = struct.unpack_from("<q", data, offset + 6)
        segments = data[offset + 26]
        table_at = offset + 27
        body_at = table_at + segments
        if body_at > len(data):
            break
        body_length = sum(data[table_at:body_at])

        if pre_skip is None and data[body_at : body_at + 8] == b"OpusHead":
            (pre_skip,) = struct.unpack_from("<H", data, body_at + 10)
        # -1 marks a page on which no packet finishes; it is not a position.
        if granule >= 0:
            final_granule = granule
        offset = body_at + body_length

    if pre_skip is None:
        raise AudioDurationError(
            "The returned OGG_OPUS audio carries no `OpusHead`, so its duration cannot be "
            "measured. Report this rather than retrying."
        )
    if final_granule is None:
        raise AudioDurationError(
            "The returned OGG_OPUS audio carries no completed page, so it holds no playable "
            "audio. Report this rather than retrying."
        )
    return max(0.0, (final_granule - pre_skip) / _OPUS_GRANULE_RATE)
