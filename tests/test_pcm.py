"""`pcm.py` — the concatenation math `openrouter_tts` depends on being exact."""

from __future__ import annotations

import struct

import pytest

from tamtree_shortvideo.audio_duration import duration_seconds
from tamtree_shortvideo.pcm import (
    BYTES_PER_SAMPLE,
    CHANNELS,
    SAMPLE_RATE_HZ,
    PcmError,
    pcm_duration_seconds,
    silence,
    wrap_pcm_as_wav,
)


def _pcm(seconds: float) -> bytes:
    sample_count = round(seconds * SAMPLE_RATE_HZ)
    return b"\x01\x00" * sample_count


def test_duration_is_exact_for_a_whole_number_of_samples() -> None:
    pcm = _pcm(1.5)
    assert pcm_duration_seconds(pcm) == pytest.approx(1.5)


def test_empty_audio_is_refused() -> None:
    with pytest.raises(PcmError, match="empty"):
        pcm_duration_seconds(b"")


def test_a_dangling_byte_is_refused_rather_than_rounded() -> None:
    with pytest.raises(PcmError, match="16-bit mono samples"):
        pcm_duration_seconds(_pcm(1.0) + b"\x00")


def test_silence_is_the_right_length_and_all_zero() -> None:
    gap = silence(0.1)
    assert len(gap) == round(0.1 * SAMPLE_RATE_HZ) * BYTES_PER_SAMPLE * CHANNELS
    assert gap == b"\x00" * len(gap)


def test_zero_or_negative_silence_is_empty() -> None:
    assert silence(0.0) == b""
    assert silence(-1.0) == b""


def test_concatenation_is_just_joining_bytes() -> None:
    """The whole point of choosing PCM: no re-encoding, no frame boundaries."""
    first = _pcm(1.0)
    second = _pcm(0.5)
    joined = first + silence(0.2) + second
    assert pcm_duration_seconds(joined) == pytest.approx(1.7)


def test_wrapped_wav_is_measured_identically_by_the_shared_duration_module() -> None:
    """`google_tts` and `openrouter_tts` must agree on what a LINEAR16 file
    plays for, since the compositor cannot tell which node built it (D9)."""
    pcm = _pcm(2.0)
    wav = wrap_pcm_as_wav(pcm)
    assert duration_seconds(wav, encoding="LINEAR16") == pytest.approx(pcm_duration_seconds(pcm))


def test_wrapped_wav_carries_the_documented_format_fields() -> None:
    wav = wrap_pcm_as_wav(_pcm(1.0))
    assert wav[0:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    _fmt_tag, channels, sample_rate, byte_rate, _block_align, bits = struct.unpack_from(
        "<HHIIHH", wav, 20
    )
    assert channels == CHANNELS
    assert sample_rate == SAMPLE_RATE_HZ
    assert bits == BYTES_PER_SAMPLE * 8
    assert byte_rate == SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS
    assert wav[36:40] == b"data"
