"""V3.3b acceptance: the curated backend that applies §7's loudness targets.

Same split as `test_remotion.py`: the SDK's conformance suite checks the
family's claims, and the rest are this backend's own — chiefly that every value
reaching the filter string is a bounded number, and that it keeps the controls
`remotion` had to decline.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tamtree_plugin_sdk import CuratedBackend, CuratedCompileError
from tamtree_plugin_sdk.testing import CuratedBackendContract

from tamtree_shortvideo import AUDIO_BACKEND
from tamtree_shortvideo.loudness import (
    BACKEND,
    BACKEND_ID,
    PRESET_NAME,
    AudioToolUnavailable,
    LoudnessBackend,
    LoudnormParams,
    LoudnormPreset,
    loudness_limits,
)


def _argv(**params: object) -> list[str]:
    command = LoudnormPreset().compile(
        binary="/usr/bin/ffmpeg",
        inputs=["_in/0.wav"],
        params=LoudnormParams(**{"target_lufs": -16.0, **params}),  # type: ignore[arg-type]
    )
    return command.argv


class TestLoudnessBackendContract(CuratedBackendContract):
    """The family's own conformance suite, run against this backend."""

    def make_backend(self) -> CuratedBackend:
        return LoudnessBackend()

    def sample_params(self, preset: str) -> dict[str, object]:
        # `target_lufs` is required — a default would be a loudness decision
        # made here rather than in the timeline contract.
        return {"target_lufs": -16.0}


# --- the backend's own declarations ----------------------------------------


def test_the_module_level_backend_is_what_the_entry_point_resolves_to() -> None:
    assert BACKEND is AUDIO_BACKEND
    assert BACKEND.id == BACKEND_ID == "shortvideo-audio"
    assert set(BACKEND.presets) == {PRESET_NAME}


def test_ffmpeg_keeps_every_default_control() -> None:
    """`remotion` declines `RLIMIT_AS` because a browser cannot start under
    it. ffmpeg can, so this backend has no business declining anything."""
    assert loudness_limits().limit_address_space is True


def test_missing_ffmpeg_raises_the_plugin_side_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAMTREE_FFMPEG_BIN", raising=False)
    monkeypatch.setattr("tamtree_shortvideo.loudness.shutil.which", lambda _name: None)
    with pytest.raises(AudioToolUnavailable, match="TAMTREE_FFMPEG_BIN"):
        LoudnessBackend().resolve_binary()


def test_it_honours_the_same_ffmpeg_override_as_the_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAMTREE_FFMPEG_BIN", "/opt/ffmpeg/bin/ffmpeg")
    assert LoudnessBackend().resolve_binary() == "/opt/ffmpeg/bin/ffmpeg"


# --- the argv, which is the whole security surface -------------------------


def test_the_filter_carries_the_three_numbers_and_nothing_else() -> None:
    argv = _argv(true_peak_dbtp=-1.5)
    assert argv[argv.index("-af") + 1] == "loudnorm=I=-16:TP=-1.5:LRA=11:dual_mono=true"


def test_the_output_is_48k_pcm_so_nothing_resamples_twice() -> None:
    argv = _argv()
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
    assert "-vn" in argv
    command = LoudnormPreset().compile(
        binary="ffmpeg", inputs=["_in/0.wav"], params=LoudnormParams(target_lufs=-20.0)
    )
    assert command.output.rel_path == "output.wav"
    assert command.output.attachment == "audio"
    assert command.output.mime == "audio/wav"


@pytest.mark.parametrize(
    "knobs",
    [
        {"target_lufs": -4.0},
        {"target_lufs": -41.0},
        {"true_peak_dbtp": 0.5},
        {"loudness_range": 25.0},
        {"target_lufs": float("nan")},
        {"target_lufs": float("-inf")},
        {"target_lufs": "-16:af=volume=10"},
        {"filter": "volume=10"},
    ],
)
def test_anything_but_a_bounded_number_is_refused_before_an_argv_exists(
    knobs: dict[str, object],
) -> None:
    """NaN and infinity are the interesting rows: both format to filter tokens
    (`nan`, `-inf`) that no bound was ever checked against."""
    with pytest.raises(ValidationError):
        LoudnormParams(**{"target_lufs": -16.0, **knobs})  # type: ignore[arg-type]


def test_there_is_no_default_target() -> None:
    with pytest.raises(ValidationError):
        LoudnormParams()  # type: ignore[call-arg]


@pytest.mark.parametrize("inputs", [[], ["_in/0.wav", "_in/1.wav"]])
def test_exactly_one_input(inputs: list[str]) -> None:
    with pytest.raises(CuratedCompileError, match="exactly one"):
        LoudnormPreset().compile(
            binary="ffmpeg", inputs=inputs, params=LoudnormParams(target_lufs=-16.0)
        )
