"""Raw PCM, as `shortvideo.openrouter_tts` gets it back — wrapped, measured, joined.

**Why PCM and not MP3.** OpenRouter's `/audio/speech` offers both, but MP3 is a
framed format: joining two MP3 streams byte-for-byte does not join the audio
they decode to, and measuring an exact duration means walking every frame
(`audio_duration.py` already carries that cost once, for Google's output).
Headerless PCM has neither problem — concatenation is `b"".join(...)`, and
duration is a division. That is what makes the per-phrase-call-and-stitch
design (this node's answer to a provider with no `<mark>` timepoints) cheap
enough to do once per phrase rather than once per beat.

**Why the rate is a constant and not read from a response field.** Gemini 3.1
Flash TTS Preview's PCM output is documented as 24 kHz / 16-bit mono — a fixed
property of the model, not a request parameter OpenRouter exposes. Trusting a
documented constant here is the same call `audio_duration.py` makes about
Google's MP3 output being 32kbps: not a guess, a cited spec.
"""

from __future__ import annotations

import struct
from typing import Final

from tamtree_plugin_sdk import NodeConfigurationError

__all__ = [
    "BYTES_PER_SAMPLE",
    "CHANNELS",
    "SAMPLE_RATE_HZ",
    "PcmError",
    "pcm_duration_seconds",
    "silence",
    "wrap_pcm_as_wav",
]

#: Gemini 3.1 Flash TTS Preview's documented PCM output shape.
SAMPLE_RATE_HZ: Final = 24_000
BYTES_PER_SAMPLE: Final = 2
CHANNELS: Final = 1

_BYTE_RATE: Final = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS


class PcmError(NodeConfigurationError):
    """The returned audio is not a whole number of 16-bit mono samples.

    Non-retryable: a byte count that is not even will not become even on a
    second identical request.
    """


def pcm_duration_seconds(pcm: bytes) -> float:
    """Exact playing time, from the byte count alone.

    Exact because the format is fixed by us, not sniffed from a header that
    might be padded or lying — the same confidence `duration_seconds()` earns
    for Google's WAV by parsing `fmt `/`data`, earned here by construction
    instead.
    """
    if not pcm:
        raise PcmError(
            "OpenRouter returned an empty audio body, so there is nothing to measure. "
            "Re-running will not help; check the voice and input in this step."
        )
    if len(pcm) % (BYTES_PER_SAMPLE * CHANNELS) != 0:
        raise PcmError(
            f"OpenRouter returned {len(pcm)} bytes of PCM, which is not a whole number of "
            f"16-bit mono samples. Report this rather than retrying."
        )
    return len(pcm) / _BYTE_RATE


def silence(seconds: float) -> bytes:
    """`seconds` of digital silence in the same PCM shape, for the gap between
    two independently-synthesized phrases."""
    if seconds <= 0:
        return b""
    sample_count = round(seconds * SAMPLE_RATE_HZ)
    return b"\x00" * (sample_count * BYTES_PER_SAMPLE * CHANNELS)


def wrap_pcm_as_wav(pcm: bytes) -> bytes:
    """A minimal 44-byte canonical WAV header in front of `pcm`.

    So the rest of the pipeline — `audio_duration.duration_seconds`, the
    compositor's ffmpeg — sees the same `LINEAR16` container
    `shortvideo.google_tts` already produces, and neither has to learn a second
    audio shape for a second TTS provider (D9).
    """
    byte_rate = _BYTE_RATE
    block_align = BYTES_PER_SAMPLE * CHANNELS
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        CHANNELS,
        SAMPLE_RATE_HZ,
        byte_rate,
        block_align,
        BYTES_PER_SAMPLE * 8,
        b"data",
        data_size,
    )
    return header + pcm
