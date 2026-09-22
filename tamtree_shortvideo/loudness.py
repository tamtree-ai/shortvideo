"""`LoudnessBackend` — integrated loudness, applied before the render (V3.3b).

**Why this exists at all.** `TimelineV1` §7 freezes narration at `-16 LUFS`
integrated with a `-1.5 dBTP` true-peak ceiling, and music at `-20 LUFS`. The
renderer cannot honour either: integrated loudness is a measurement over the
whole programme, a browser has no way to take it, and Remotion exposes no
filter hook at encode time. So the numbers are applied here, to each audio
track, *before* the render sees it — and the renderer keeps doing what it can
do, which is every relative level (the clip-audio policy and the music duck).

**Why it is a backend of this plugin and not a preset on the product's ffmpeg
backend.** A preset added to `tamtree_nodes.ffmpeg` is invisible to a plugin:
nothing in the SDK says which presets an instance's ffmpeg backend carries, so
depending on one would have meant a `CONTRACTS_VERSION` bump and a pin raise
purely to make an *internal* preset discoverable — and every instance a minor
behind would refuse the whole plugin at boot. A backend contributed through
`tamtree.curated_backends` is discovered the same way `remotion` is, runs under
the same `CuratedCliToolRuntime`, and needs nothing from the product.

**What it costs the worker: nothing new.** It resolves the same ffmpeg the
product's own media node uses (`TAMTREE_FFMPEG_BIN`, else `PATH`), which is
already a self-hosted worker requirement. And unlike `remotion` it keeps every
default control, `RLIMIT_AS` included — ffmpeg is an ordinary native process
and starts fine under the cap.

**Single-pass, and what that means for accuracy.** A preset compiles to *one*
argv, so the two-pass form (measure, then apply linearly with the measured
values) is not expressible without a second invocation and a way to read the
first one's stderr back. Single-pass `loudnorm` runs in its dynamic mode and
lands within roughly ±1 LU of target on speech; the true-peak ceiling is
enforced by its limiter either way. That is recorded as the v1 tolerance rather
than presented as exact.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from tamtree_plugin_sdk import (
    CompiledCommand,
    CuratedCompileError,
    MediaLimits,
    Preset,
    PresetOutput,
)

__all__ = [
    "BACKEND_ID",
    "PRESET_NAME",
    "AudioToolUnavailable",
    "LoudnessBackend",
    "LoudnormParams",
    "LoudnormPreset",
    "loudness_limits",
]

#: Hyphenated and plugin-prefixed so it cannot collide with a first-party
#: backend id — the registry refuses a second backend claiming an id.
BACKEND_ID: Final = "shortvideo-audio"
PRESET_NAME: Final = "loudnorm"

#: The same override the product's ffmpeg backend honours, so an operator who
#: pinned one ffmpeg build has pinned it for both.
_FFMPEG_BIN_ENV: Final = "TAMTREE_FFMPEG_BIN"

#: The output is always 48 kHz 16-bit PCM — the rate the renderer's AAC encode
#: runs at, so nothing resamples twice. 180s of stereo at that rate is ~35 MB;
#: 256 MB is the bomb guard.
_SAMPLE_RATE: Final = 48000
_DEFAULT_FSIZE: Final = 256 * 1024 * 1024

#: CPU-seconds. `loudnorm` upsamples to 192 kHz internally for true-peak
#: detection, so it is not free, but 180s of narration is a few CPU-seconds.
_DEFAULT_TIMEOUT_S: Final = 120
_DEFAULT_MEMORY_MB: Final = 512

_TIMEOUT_ENV: Final = "TAMTREE_SHORTVIDEO_AUDIO_TIMEOUT_S"
_FSIZE_ENV: Final = "TAMTREE_SHORTVIDEO_AUDIO_FSIZE"


class AudioToolUnavailable(RuntimeError):
    """ffmpeg is not installed on this worker. The plugin-side spelling of
    `MissingRuntime`, for the same reason as `RendererUnavailable`."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default


def loudness_limits() -> MediaLimits:
    return MediaLimits(
        fsize_bytes=_env_int(_FSIZE_ENV, _DEFAULT_FSIZE),
        timeout_s=_env_int(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S),
        memory_mb=_DEFAULT_MEMORY_MB,
    )


class LoudnormParams(BaseModel):
    """The knob surface — three bounded floats, and that is the security
    boundary: every value interpolated into the filter string is one of them.
    `allow_inf_nan=False` because `f"{nan:g}"` is the string `nan`, which is a
    filter token nobody validated."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    target_lufs: float = Field(ge=-40.0, le=-5.0)
    true_peak_dbtp: float = Field(default=-1.5, ge=-9.0, le=0.0)
    #: Loudness range. 11 LU is ffmpeg's default and leaves speech its natural
    #: dynamics; it is a knob only so the default is written down, not assumed.
    loudness_range: float = Field(default=11.0, ge=1.0, le=20.0)


class LoudnormPreset:
    """Normalise one audio input to an integrated-loudness target."""

    # Plain class attributes (not ClassVar) so this structurally satisfies the
    # `Preset` protocol's instance-variable members.
    name: str = PRESET_NAME
    params_model: type[BaseModel] = LoudnormParams

    def compile(self, *, binary: str, inputs: Sequence[str], params: BaseModel) -> CompiledCommand:
        if not isinstance(params, LoudnormParams):  # pragma: no cover - runtime validates
            raise CuratedCompileError("loudnorm params were not validated")
        if len(inputs) != 1:
            raise CuratedCompileError(
                f"loudnorm normalises exactly one audio input, got {len(inputs)}"
            )

        # Mono is measured as **one channel** — no `dual_mono` — and that was
        # measured, not assumed (V3.4 image smoke). TTS narration is mono and
        # the render is stereo, but Remotion upmixes mono at −3 dB per channel
        # (equal-power), and BS.1770 sums channel power, so a mono track at
        # −16 LUFS comes out of the render at −16 LUFS. `dual_mono=true`
        # assumes the upmix duplicates at unity instead, and landed the
        # finished video at −19. `renderer/smoke/run.py` asserts on the
        # rendered mix, so a change in Remotion's upmix fails there.
        audio_filter = (
            f"loudnorm=I={params.target_lufs:g}:TP={params.true_peak_dbtp:g}"
            f":LRA={params.loudness_range:g}"
        )
        out_rel = "output.wav"
        argv = [
            binary,
            "-nostdin",
            "-y",
            "-i",
            inputs[0],
            # Audio only — a video input's picture is not this preset's business.
            "-vn",
            "-af",
            audio_filter,
            # loudnorm resamples to 192 kHz internally and outputs at that rate
            # unless told otherwise.
            "-ar",
            str(_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            out_rel,
        ]
        return CompiledCommand(
            argv=argv,
            output=PresetOutput(attachment="audio", rel_path=out_rel, mime="audio/wav"),
        )


class LoudnessBackend:
    """The `CuratedBackend` that applies §7's loudness targets."""

    id: str = BACKEND_ID  # plain attribute, so the Protocol matches structurally

    def __init__(self, limits: MediaLimits | None = None) -> None:
        self.limits: MediaLimits = limits or loudness_limits()
        self.presets: Mapping[str, Preset] = {PRESET_NAME: LoudnormPreset()}

    def resolve_binary(self) -> str:
        binary = os.environ.get(_FFMPEG_BIN_ENV) or shutil.which("ffmpeg")
        if not binary:
            raise AudioToolUnavailable(
                "Short video compose needs ffmpeg on the worker to normalise narration "
                f"loudness. Install it, or set {_FFMPEG_BIN_ENV} to its path."
            )
        return binary


#: The instance the `tamtree.curated_backends` entry point resolves to.
BACKEND: Final = LoudnessBackend()
