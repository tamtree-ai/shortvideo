"""Duration measurement — the number the whole timeline is built on.

Every assertion here compares a measurement against a duration that is known
by construction (`audio_fixtures`), not against another measurement. A parser
that agreed with itself would be no evidence at all.
"""

from __future__ import annotations

import pytest
from tamtree_plugin_sdk import NodeConfigurationError

from tamtree_shortvideo.audio_duration import (
    MEASURABLE_ENCODINGS,
    AudioDurationError,
    duration_seconds,
)
from tests.audio_fixtures import (
    MP3_FRAME_SECONDS,
    OPUS_GRANULE_RATE,
    OPUS_PRE_SKIP,
    mp3_bytes,
    mp3_frame_header,
    ogg_opus_bytes,
    wav_bytes,
)

# -- WAV ---------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0.25, 1.0, 3.5])
@pytest.mark.parametrize("sample_rate", [16_000, 24_000, 44_100])
def test_wav_duration_is_exact_at_every_rate(seconds: float, sample_rate: int) -> None:
    audio = wav_bytes(seconds=seconds, sample_rate=sample_rate)

    assert duration_seconds(audio, encoding="LINEAR16") == pytest.approx(seconds, abs=1e-6)


def test_a_stereo_file_is_not_measured_as_twice_as_long() -> None:
    """Stereo doubles the payload and the byte rate together. Dividing by the
    payload alone — the obvious wrong implementation — would report 2.0s."""
    stereo = wav_bytes(seconds=1.0, channels=2)
    mono = wav_bytes(seconds=1.0, channels=1)

    assert len(stereo) > len(mono) * 1.9  # the payload really did double
    assert duration_seconds(stereo, encoding="LINEAR16") == pytest.approx(1.0, abs=1e-6)


def test_wav_trusts_the_bytes_in_hand_over_a_streamed_length_declaration() -> None:
    """A streamed WAV can declare `0xFFFFFFFF` in its `data` chunk. Believing
    that would report a duration of several days."""
    audio = bytearray(wav_bytes(seconds=0.5))
    data_at = audio.find(b"data")
    audio[data_at + 4 : data_at + 8] = (0xFFFFFFFF).to_bytes(4, "little")

    assert duration_seconds(bytes(audio), encoding="LINEAR16") == pytest.approx(0.5, abs=1e-6)


def test_a_linear16_response_without_its_wav_header_is_named_not_guessed() -> None:
    with pytest.raises(AudioDurationError, match="without the WAV header"):
        duration_seconds(b"\x00" * 4_000, encoding="LINEAR16")


def test_a_wav_with_no_data_chunk_is_named() -> None:
    audio = wav_bytes(seconds=0.1)
    header_only = audio[: audio.find(b"data")]

    with pytest.raises(AudioDurationError, match="no `data` chunk"):
        duration_seconds(header_only, encoding="LINEAR16")


# -- MP3 ---------------------------------------------------------------------


@pytest.mark.parametrize("frames", [1, 20, 417])
def test_mp3_duration_is_the_sum_of_its_frames(frames: int) -> None:
    audio = mp3_bytes(frames=frames)

    measured = duration_seconds(audio, encoding="MP3")

    assert measured == pytest.approx(frames * MP3_FRAME_SECONDS, abs=1e-9)


def test_a_leading_id3_tag_is_skipped_rather_than_read_as_audio() -> None:
    plain = duration_seconds(mp3_bytes(frames=10), encoding="MP3")
    tagged = duration_seconds(mp3_bytes(frames=10, id3=True), encoding="MP3")

    assert tagged == pytest.approx(plain, abs=1e-9)


def test_a_xing_header_frame_does_not_add_its_own_silence() -> None:
    """The VBR header is a real frame carrying no audio. Counting it would add
    24ms to every measurement — a frame of drift at 30fps, every beat."""
    plain = duration_seconds(mp3_bytes(frames=10), encoding="MP3")
    with_xing = duration_seconds(mp3_bytes(frames=10, xing=True), encoding="MP3")

    assert with_xing == pytest.approx(plain, abs=1e-9)


def test_a_trailing_id3v1_tag_does_not_fail_the_measurement() -> None:
    audio = mp3_bytes(frames=10) + b"TAG" + b"\x00" * 125

    assert duration_seconds(audio, encoding="MP3") == pytest.approx(
        10 * MP3_FRAME_SECONDS, abs=1e-9
    )


def test_a_truncated_mp3_is_refused_rather_than_measured_short() -> None:
    """Silently measuring the readable prefix is the dangerous answer: the
    timeline would be built on a duration shorter than the narration."""
    audio = mp3_bytes(frames=10) + b"\x11" * 4_096

    with pytest.raises(AudioDurationError, match="stops being readable"):
        duration_seconds(audio, encoding="MP3")


def test_mp3_with_no_readable_frame_is_named() -> None:
    with pytest.raises(AudioDurationError, match="no readable MPEG audio frame"):
        duration_seconds(b"\x11" * 200, encoding="MP3")


def test_a_free_format_frame_is_refused_rather_than_divided_by_zero() -> None:
    """Bitrate index 0 means "the bitrate is not in the header". There is
    nothing to compute a frame length from, so the walk must stop."""
    header = int.from_bytes(mp3_frame_header(), "big")
    free_format = (header & ~(0b1111 << 12)).to_bytes(4, "big")

    with pytest.raises(AudioDurationError, match="no readable MPEG audio frame"):
        duration_seconds(free_format + b"\x00" * 92, encoding="MP3")


# -- Ogg Opus ----------------------------------------------------------------


@pytest.mark.parametrize("samples", [4_800, 48_000, 123_456])
def test_ogg_opus_duration_comes_from_the_final_granule(samples: int) -> None:
    audio = ogg_opus_bytes(samples=samples)

    measured = duration_seconds(audio, encoding="OGG_OPUS")

    assert measured == pytest.approx(samples / OPUS_GRANULE_RATE, abs=1e-9)


def test_the_pre_skip_is_subtracted_not_played() -> None:
    """Priming samples are decoded and discarded. Reading the granule position
    on its own would report every Opus narration longer than it sounds.

    Both streams below end on the *same* granule, so the only thing that can
    make their measurements differ is the pre-skip.
    """
    from tests.audio_fixtures import ogg_page

    def stream(pre_skip: int) -> bytes:
        head = (
            b"OpusHead"
            + bytes([1, 1])
            + pre_skip.to_bytes(2, "little")
            + (48_000).to_bytes(4, "little")
            + (0).to_bytes(2, "little")
            + bytes([0])
        )
        return ogg_page(head, granule=0, flags=0x02, seq=0) + ogg_page(
            b"\x00" * 40, granule=48_000, flags=0x04, seq=1
        )

    assert duration_seconds(stream(0), encoding="OGG_OPUS") == pytest.approx(1.0, abs=1e-9)
    assert duration_seconds(stream(OPUS_PRE_SKIP), encoding="OGG_OPUS") == pytest.approx(
        (48_000 - OPUS_PRE_SKIP) / OPUS_GRANULE_RATE, abs=1e-9
    )


def test_ogg_without_an_opushead_is_named() -> None:
    from tests.audio_fixtures import ogg_page

    audio = ogg_page(b"\x00" * 20, granule=48_000, flags=0x02, seq=0)

    with pytest.raises(AudioDurationError, match="no `OpusHead`"):
        duration_seconds(audio, encoding="OGG_OPUS")


def test_bytes_that_are_not_an_ogg_stream_are_named() -> None:
    with pytest.raises(AudioDurationError, match="not a readable Ogg stream"):
        duration_seconds(b"\x11" * 200, encoding="OGG_OPUS")


# -- the shared contract -----------------------------------------------------


def test_empty_audio_is_named() -> None:
    with pytest.raises(AudioDurationError, match="empty audio body"):
        duration_seconds(b"", encoding="LINEAR16")


def test_an_unmeasurable_encoding_is_refused_by_name() -> None:
    """`PCM` is `LINEAR16` without the header; `M4A` needs an MP4 box parser.
    Neither can be measured here, so neither is offered."""
    with pytest.raises(AudioDurationError, match="Cannot measure"):
        duration_seconds(b"\x00" * 1_000, encoding="PCM")


def test_every_offered_encoding_is_actually_measurable() -> None:
    """The list the node's dropdown is built from and the list this module
    dispatches on are the same list."""
    samples = {
        "LINEAR16": wav_bytes(seconds=0.1),
        "MP3": mp3_bytes(frames=4),
        "OGG_OPUS": ogg_opus_bytes(samples=4_800),
    }
    assert set(samples) == set(MEASURABLE_ENCODINGS)
    for encoding, audio in samples.items():
        assert duration_seconds(audio, encoding=encoding) > 0, encoding


def test_every_failure_is_non_retryable() -> None:
    """The synthesis has already been billed; a retry pays again for the same
    unreadable bytes. `NodeConfigurationError` is what marks a step
    non-retryable and carries its sentence to the run banner."""
    assert issubclass(AudioDurationError, NodeConfigurationError)
