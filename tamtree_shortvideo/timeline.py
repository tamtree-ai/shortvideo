"""`TimelineV1` — the frozen composition contract (V3.1).

The complete, deterministic description of one rendered short: which artifact
plays when, what is said over it, how loud, and for exactly how many frames.
It exists because D1 forbids the alternative — a workflow cannot hand Remotion
TSX, and captions cannot ride argv — so **everything variable about a render is
data in this document and everything executable is first-party and fixed**.

The freeze, with its reasoning, is `02-timeline-v1.md` in the planning
subproject. This module is where the freeze is enforced; the two are meant to
be read together, and a rule changed here without a line changed there is a
drift, not a fix.

**Two forms, and the difference is load-bearing.** The *authoring* form is what
`shortvideo.compose` builds and validates, and it names artifacts by `BinaryRef`
id. The *render document* is what gets materialized into the sandbox workdir,
and it names them by runtime-generated relative paths — `clip-000.mp4`,
`narration.wav`. The child never learns a ref id, a workspace id or an original
file name. `timeline_digest` hashes the **authoring** form, because "the user
approved *these* artifacts" (V4.3) is a statement about workspace binaries, not
about temporary paths that differ between the draft render and the final one.

**Nothing here is optional-with-a-guess.** `total_frames`, `start_frame` and
`frames` are derived by the builder and re-derived by `validate`; a value that
disagrees is a rejection rather than a correction. That is what makes "the same
`TimelineV1` renders deterministically" checkable instead of hoped for.

**Audio drives the timeline, not the video** (§3 of the plan). Beat boundaries
come from the narration's `<mark>` timepoints; a clip is trimmed to its beat,
and a beat is never stretched to its clip.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from tamtree_plugin_sdk import NodeConfigurationError

__all__ = [
    "CAPTION_SAFE_AREA",
    "FRAME_HEIGHT",
    "FRAME_RATE",
    "FRAME_WIDTH",
    "CLIP_DUCK_DB",
    "MAX_BEATS",
    "MAX_BEAT_SECONDS",
    "MAX_CAPTIONS_PER_BEAT",
    "MAX_CAPTION_CHARACTERS",
    "MAX_PAD_SECONDS",
    "MAX_TOTAL_SECONDS",
    "MAX_TRANSITION_FRAMES",
    "MAX_TRANSITION_SHARE",
    "MIN_BEAT_SECONDS",
    "MIN_TOTAL_SECONDS",
    "MUSIC_DUCK_ATTACK_MS",
    "MUSIC_DUCK_DB",
    "MUSIC_DUCK_RELEASE_MS",
    "MUSIC_TARGET_LUFS",
    "NARRATION_TARGET_LUFS",
    "NARRATION_TRUE_PEAK_DBTP",
    "SUPPORTED_AUDIO_TYPES",
    "SUPPORTED_VIDEO_TYPES",
    "TEMPLATES",
    "TIMELINE_VERSION",
    "Beat",
    "Caption",
    "Clip",
    "Music",
    "Narration",
    "Timeline",
    "TimelineError",
    "Transition",
    "as_json",
    "beat_boundaries",
    "canonical_json",
    "frames_at",
    "render_document",
    "timeline_digest",
    "timeline_from_json",
    "validate",
]

TIMELINE_VERSION: Final = 1

#: The two first-party templates, bundled at image-build time (D1/D2). The id
#: reaches the render child as a fixed argv token, never as text — which is why
#: it is an enum here and not a string.
TEMPLATES: Final = ("short-captioned", "short-plain")

#: v1 renders one shape. The fields exist on the timeline so a 60fps or square
#: variant needs no `TimelineV2`; the validator accepts only these values today.
FRAME_WIDTH: Final = 1080
FRAME_HEIGHT: Final = 1920
FRAME_RATE: Final = 30

#: §8 of the freeze. Validation limits, checked before anything is materialized
#: and long before Chrome exists — D9's "reject before spawning Chrome".
MIN_TOTAL_SECONDS: Final = 3.0
MAX_TOTAL_SECONDS: Final = 180.0
MAX_BEATS: Final = 30
MIN_BEAT_SECONDS: Final = 1.0
#: MiniMax H3's own maximum. A longer beat cannot be generated, so it cannot
#: have arrived honestly.
MAX_BEAT_SECONDS: Final = 15.0
MAX_PAD_SECONDS: Final = 0.5
MAX_TRANSITION_FRAMES: Final = 15
#: A transition may not eat more than this share of either neighbouring beat —
#: a half-second dissolve across a 1.2s beat is a smear, not a cut.
MAX_TRANSITION_SHARE: Final = 0.25
MAX_CAPTION_CHARACTERS: Final = 90
MAX_CAPTIONS_PER_BEAT: Final = 6

#: §7. Frozen numbers rather than defaults chosen at render time. -16 LUFS is
#: the short-form convention and leaves headroom -14 does not once a music bed
#: is under it; -1.5 dBTP survives the destination's lossy re-encode without
#: inter-sample clipping.
NARRATION_TARGET_LUFS: Final = -16.0
NARRATION_TRUE_PEAK_DBTP: Final = -1.5
CLIP_DUCK_DB: Final = -18.0
MUSIC_TARGET_LUFS: Final = -20.0
MUSIC_DUCK_DB: Final = -12.0
MUSIC_DUCK_ATTACK_MS: Final = 150
MUSIC_DUCK_RELEASE_MS: Final = 400

#: What `minimax_collect` produces, and nothing else.
SUPPORTED_VIDEO_TYPES: Final = ("video/mp4", "video/webm")
#: What Wave 1's two narration nodes produce.
SUPPORTED_AUDIO_TYPES: Final = ("audio/wav", "audio/mpeg", "audio/ogg", "audio/L16")

#: The caption band, as fractions of the frame. Below `bottom` is where every
#: short-form surface puts its own chrome — handle, description, progress bar,
#: CTA — and above `top` is the frame's subject. A template constant, not a
#: timeline field: the timeline says *what* the caption is and *when*, never
#: where.
CAPTION_SAFE_AREA: Final = {
    "inset_x": 0.08,
    "top": 0.66,
    "bottom": 0.84,
}

_CLIP_AUDIO_POLICIES: Final = ("mute", "duck", "keep")
_TRANSITION_KINDS: Final = ("none", "crossfade")

#: Seconds compare to within a microsecond. Every duration in play is a
#: measured media length or a frame count divided by fps, so anything looser
#: would be hiding a real disagreement.
_EPSILON: Final = 1e-6


class TimelineError(NodeConfigurationError):
    """The timeline is not renderable, and says exactly which rule it broke.

    Non-retryable by construction: nothing about a second identical attempt
    would differ, and no provider was called.
    """


@dataclass(frozen=True, slots=True)
class Caption:
    """One caption, timed in frames against the timeline's own clock."""

    text: str
    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class Transition:
    """How the render arrives at the beat carrying it.

    A transition **never alters a beat boundary**: a crossfade is drawn by
    extending the *outgoing* clip past its `out_seconds`, into the tail the
    trim deliberately kept, and dissolving it over the first `frames` of this
    beat. The narration underneath never moves, which is why a draft render and
    a final render of the same timeline cut in the same places.
    """

    kind: Literal["none", "crossfade"] = "none"
    frames: int = 0


@dataclass(frozen=True, slots=True)
class Clip:
    """The generated footage for one beat, and the window taken from it."""

    ref_id: str
    mime_type: str
    source_duration_seconds: float
    in_seconds: float
    out_seconds: float


@dataclass(frozen=True, slots=True)
class Beat:
    """One narration phrase, its footage, and its captions.

    `pad_frames` is the tail held on the clip's last frame when the provider
    returned marginally less than it was asked for. It is recorded rather than
    inferred so the render document never has to guess, and it is bounded at
    half a second: a beat visibly frozen for longer is a defect a user should
    meet as a validation error, not as a still frame in a finished render.
    """

    index: int
    start_frame: int
    frames: int
    clip: Clip
    transition: Transition = Transition()
    captions: tuple[Caption, ...] = ()
    pad_frames: int = 0


@dataclass(frozen=True, slots=True)
class Narration:
    """The single narration track the whole timeline is timed against."""

    ref_id: str
    mime_type: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class Music:
    """An optional bed. Neither shipped template supplies one in v1 — the
    fields exist because the mix graph has to be decided once, and deciding it
    later would be a contract change rather than a template change."""

    ref_id: str
    mime_type: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class Timeline:
    """The authoring form. `validate` is what makes an instance trustworthy."""

    template: Literal["short-captioned", "short-plain"]
    narration: Narration
    beats: tuple[Beat, ...]
    clip_audio: Literal["mute", "duck", "keep"] = "mute"
    music: Music | None = None
    width: int = FRAME_WIDTH
    height: int = FRAME_HEIGHT
    fps: int = FRAME_RATE
    version: int = TIMELINE_VERSION

    @property
    def total_frames(self) -> int:
        return sum(beat.frames for beat in self.beats)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / self.fps


def frames_at(seconds: float, fps: int = FRAME_RATE) -> int:
    """Seconds → frames, half-up, evaluated once per **absolute** boundary.

    Summing per-beat durations and rounding each would drift by up to half a
    frame per beat; deriving every boundary from its own absolute time cannot.
    That is the whole reason this is a function and not an inline expression.
    """
    if not math.isfinite(seconds) or seconds < 0:
        raise TimelineError(
            f"a timeline boundary must be a finite, non-negative time — got {seconds!r}"
        )
    return math.floor(seconds * fps + 0.5)


def beat_boundaries(
    mark_times: Sequence[float],
    *,
    duration_seconds: float,
    fps: int = FRAME_RATE,
) -> tuple[tuple[int, int], ...]:
    """Narration `<mark>` timepoints → `(start_frame, frames)` per beat.

    The frame math of the freeze, in one place, so V3.3's compose and V4.2's
    aggregation cannot derive it two subtly different ways. Beat *i* spans
    `[mark_i, mark_{i+1})` and the last runs to the measured narration end;
    **beat 0 starts at frame 0 regardless of its mark**, absorbing any lead-in
    silence rather than leaving a gap nothing plays over.
    """
    if not mark_times:
        raise TimelineError("a timeline needs at least one narration mark to derive beats from")
    if duration_seconds <= 0 or not math.isfinite(duration_seconds):
        raise TimelineError(
            "narration duration must be a positive, finite number of seconds — "
            f"got {duration_seconds!r}"
        )
    previous = -math.inf
    for position, mark in enumerate(mark_times):
        if not math.isfinite(mark) or mark < 0:
            raise TimelineError(f"narration mark {position} is not a valid time: {mark!r}")
        if mark <= previous:
            raise TimelineError(
                f"narration marks must increase strictly — mark {position} at {mark}s "
                f"follows {previous}s"
            )
        if mark >= duration_seconds:
            raise TimelineError(
                f"narration mark {position} at {mark}s is at or past the measured end "
                f"({duration_seconds}s); the audio is shorter than its own marks claim"
            )
        previous = mark

    edges = [
        0,
        *(frames_at(mark, fps) for mark in mark_times[1:]),
        frames_at(duration_seconds, fps),
    ]
    boundaries: list[tuple[int, int]] = []
    for index in range(len(edges) - 1):
        frames = edges[index + 1] - edges[index]
        if frames <= 0:
            raise TimelineError(
                f"beat {index} would be {frames} frames long — two narration marks round "
                f"to the same frame"
            )
        boundaries.append((edges[index], frames))
    return tuple(boundaries)


def validate(timeline: Timeline) -> None:
    """Raise `TimelineError` on the first rule the timeline breaks.

    Everything D9 names — missing refs aside, which only the node can resolve —
    is checked here: non-monotonic marks, overlaps, unsupported codecs, a
    duration above the ceiling, a transition a clip cannot pay for.
    """
    _validate_frame(timeline)
    _validate_narration(timeline)
    _validate_beats(timeline)
    _validate_duration(timeline)
    _validate_music(timeline)


def _validate_frame(timeline: Timeline) -> None:
    if timeline.version != TIMELINE_VERSION:
        raise TimelineError(
            f"this build renders TimelineV{TIMELINE_VERSION}, not version {timeline.version!r}"
        )
    if timeline.template not in TEMPLATES:
        raise TimelineError(
            f"unknown template {timeline.template!r} — v1 ships {' and '.join(TEMPLATES)}"
        )
    if (timeline.width, timeline.height) != (FRAME_WIDTH, FRAME_HEIGHT):
        raise TimelineError(
            f"v1 renders {FRAME_WIDTH}x{FRAME_HEIGHT} only — got {timeline.width}x{timeline.height}"
        )
    if timeline.fps != FRAME_RATE:
        raise TimelineError(f"v1 renders at {FRAME_RATE}fps only — got {timeline.fps}")
    if timeline.clip_audio not in _CLIP_AUDIO_POLICIES:
        raise TimelineError(
            f"clip audio policy must be one of {', '.join(_CLIP_AUDIO_POLICIES)} — "
            f"got {timeline.clip_audio!r}"
        )


def _validate_narration(timeline: Timeline) -> None:
    narration = timeline.narration
    if not narration.ref_id:
        raise TimelineError("the narration track has no attachment reference")
    if narration.mime_type not in SUPPORTED_AUDIO_TYPES:
        raise TimelineError(
            f"narration is {narration.mime_type!r}; composition accepts "
            f"{', '.join(SUPPORTED_AUDIO_TYPES)}"
        )
    if narration.duration_seconds <= 0 or not math.isfinite(narration.duration_seconds):
        raise TimelineError(
            f"narration duration must be positive — got {narration.duration_seconds!r}"
        )


def _validate_beats(timeline: Timeline) -> None:
    beats = timeline.beats
    fps = timeline.fps
    if not beats:
        raise TimelineError("a timeline with no beats renders nothing")
    if len(beats) > MAX_BEATS:
        raise TimelineError(f"{len(beats)} beats exceeds the v1 ceiling of {MAX_BEATS}")

    seen_refs: dict[str, int] = {}
    expected_start = 0
    for position, beat in enumerate(beats):
        if beat.index != position:
            raise TimelineError(
                f"beat at position {position} carries index {beat.index}; "
                f"beats are numbered in order"
            )
        if beat.start_frame != expected_start:
            raise TimelineError(
                f"beat {position} starts at frame {beat.start_frame}, but the beat before it ends "
                f"at {expected_start} — beats tile the timeline with no gap and no overlap"
            )
        if beat.frames <= 0:
            raise TimelineError(f"beat {position} is {beat.frames} frames long")
        seconds = beat.frames / fps
        if seconds < MIN_BEAT_SECONDS - _EPSILON:
            raise TimelineError(
                f"beat {position} is {seconds:.3f}s; the minimum is {MIN_BEAT_SECONDS}s"
            )
        if seconds > MAX_BEAT_SECONDS + _EPSILON:
            raise TimelineError(
                f"beat {position} is {seconds:.3f}s; MiniMax H3 cannot generate more than "
                f"{MAX_BEAT_SECONDS}s, so this beat has no honest source"
            )
        _validate_clip(beat, fps=fps)
        first_seen = seen_refs.setdefault(beat.clip.ref_id, position)
        if first_seen != position:
            raise TimelineError(
                f"beats {first_seen} and {position} use the same clip — almost always an "
                f"aggregation bug rather than an intention"
            )
        _validate_captions(beat, template=timeline.template)
        expected_start += beat.frames

    _validate_transitions(beats, fps=fps)

    total = frames_at(timeline.narration.duration_seconds, fps)
    if expected_start != total:
        raise TimelineError(
            f"the beats cover {expected_start} frames but the narration is {total} frames long; "
            f"the timeline is timed against the audio, so the two must agree exactly"
        )


def _validate_clip(beat: Beat, *, fps: int) -> None:
    clip = beat.clip
    where = f"beat {beat.index}"
    if not clip.ref_id:
        raise TimelineError(f"{where} has no clip attachment reference")
    if clip.mime_type not in SUPPORTED_VIDEO_TYPES:
        raise TimelineError(
            f"{where}'s clip is {clip.mime_type!r}; composition accepts "
            f"{', '.join(SUPPORTED_VIDEO_TYPES)}"
        )
    if clip.source_duration_seconds <= 0 or not math.isfinite(clip.source_duration_seconds):
        raise TimelineError(f"{where}'s clip has no measured duration")
    if clip.in_seconds < 0 or not math.isfinite(clip.in_seconds):
        raise TimelineError(f"{where}'s clip starts at {clip.in_seconds!r}")
    if clip.out_seconds <= clip.in_seconds:
        raise TimelineError(
            f"{where}'s clip window is empty: in {clip.in_seconds}s, out {clip.out_seconds}s"
        )
    if clip.out_seconds > clip.source_duration_seconds + _EPSILON:
        raise TimelineError(
            f"{where} trims to {clip.out_seconds}s but the clip is only "
            f"{clip.source_duration_seconds}s long"
        )
    if beat.pad_frames < 0:
        raise TimelineError(f"{where} carries a negative pad")
    if beat.pad_frames >= beat.frames:
        raise TimelineError(f"{where} is entirely pad ({beat.pad_frames} of {beat.frames} frames)")
    if beat.pad_frames > MAX_PAD_SECONDS * fps + _EPSILON:
        raise TimelineError(
            f"{where} would hold its last frame for {beat.pad_frames / fps:.3f}s; the ceiling is "
            f"{MAX_PAD_SECONDS}s. The clip is too short for the narration it has to cover"
        )
    window = (beat.frames - beat.pad_frames) / fps
    if abs((clip.out_seconds - clip.in_seconds) - window) > _EPSILON:
        raise TimelineError(
            f"{where} plays {clip.out_seconds - clip.in_seconds:.6f}s of footage across "
            f"{window:.6f}s of timeline; trim and frame count must agree"
        )


def _validate_transitions(beats: Sequence[Beat], *, fps: int) -> None:
    for position, beat in enumerate(beats):
        transition = beat.transition
        if transition.kind not in _TRANSITION_KINDS:
            raise TimelineError(
                f"beat {position} asks for a {transition.kind!r} transition; v1 renders "
                f"{' and '.join(_TRANSITION_KINDS)}"
            )
        if transition.kind == "none":
            if transition.frames:
                raise TimelineError(
                    f"beat {position} has no transition but claims "
                    f"{transition.frames} frames of one"
                )
            continue
        if position == 0:
            raise TimelineError("beat 0 cannot cross-fade — there is nothing to dissolve from")
        if transition.frames < 1 or transition.frames > MAX_TRANSITION_FRAMES:
            raise TimelineError(
                f"beat {position}'s crossfade is {transition.frames} frames; v1 allows 1 to "
                f"{MAX_TRANSITION_FRAMES}"
            )
        previous = beats[position - 1]
        share = MAX_TRANSITION_SHARE
        if transition.frames > share * beat.frames or transition.frames > share * previous.frames:
            raise TimelineError(
                f"beat {position}'s crossfade of {transition.frames} frames covers more than "
                f"{share:.0%} of a {min(beat.frames, previous.frames)}-frame beat — "
                f"a smear, not a cut"
            )
        tail = previous.clip.source_duration_seconds - previous.clip.out_seconds
        if tail < transition.frames / fps - _EPSILON:
            raise TimelineError(
                f"beat {position}'s crossfade needs {transition.frames / fps:.3f}s of footage past "
                f"beat {position - 1}'s out point, and only {max(tail, 0.0):.3f}s exists"
            )


def _validate_captions(beat: Beat, *, template: str) -> None:
    where = f"beat {beat.index}"
    captions = beat.captions
    if template == "short-plain":
        if captions:
            raise TimelineError(
                f"{where} carries captions, but 'short-plain' renders none — the timeline and the "
                f"template disagree about what the viewer will see"
            )
        return
    if not captions:
        raise TimelineError(f"{where} has no caption, and 'short-captioned' requires one per beat")
    if len(captions) > MAX_CAPTIONS_PER_BEAT:
        raise TimelineError(
            f"{where} has {len(captions)} captions; the ceiling is {MAX_CAPTIONS_PER_BEAT}"
        )

    end_of_beat = beat.start_frame + beat.frames
    previous_end = beat.start_frame
    for position, caption in enumerate(captions):
        _validate_caption_text(caption.text, where=f"{where}, caption {position}")
        if caption.end_frame <= caption.start_frame:
            raise TimelineError(
                f"{where}, caption {position} ends at frame {caption.end_frame}, at or before its "
                f"start ({caption.start_frame})"
            )
        if caption.start_frame < previous_end:
            raise TimelineError(
                f"{where}, caption {position} starts at frame {caption.start_frame}, overlapping "
                f"what came before it (ends {previous_end})"
            )
        if caption.end_frame > end_of_beat:
            raise TimelineError(
                f"{where}, caption {position} runs to frame {caption.end_frame}, past the end of "
                f"its own beat ({end_of_beat})"
            )
        previous_end = caption.end_frame


def _validate_caption_text(text: str, *, where: str) -> None:
    if not text.strip():
        raise TimelineError(f"{where} is empty")
    if len(text) > MAX_CAPTION_CHARACTERS:
        raise TimelineError(
            f"{where} is {len(text)} characters; the ceiling is {MAX_CAPTION_CHARACTERS}. "
            f"A caption slot is two comfortable lines, not a paragraph"
        )
    for character in text:
        if character == "\n":
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise TimelineError(
                f"{where} contains a control or surrogate character (U+{ord(character):04X})"
            )


def _validate_duration(timeline: Timeline) -> None:
    seconds = timeline.total_seconds
    if seconds < MIN_TOTAL_SECONDS - _EPSILON:
        raise TimelineError(
            f"the timeline is {seconds:.3f}s; below {MIN_TOTAL_SECONDS}s "
            f"there is nothing to approve"
        )
    if seconds > MAX_TOTAL_SECONDS + _EPSILON:
        raise TimelineError(
            f"the timeline is {seconds:.3f}s; the v1 ceiling is {MAX_TOTAL_SECONDS}s"
        )


def _validate_music(timeline: Timeline) -> None:
    music = timeline.music
    if music is None:
        return
    if not music.ref_id:
        raise TimelineError("the music bed has no attachment reference")
    if music.mime_type not in SUPPORTED_AUDIO_TYPES:
        raise TimelineError(
            f"music is {music.mime_type!r}; composition accepts {', '.join(SUPPORTED_AUDIO_TYPES)}"
        )
    if music.duration_seconds + _EPSILON < timeline.total_seconds:
        raise TimelineError(
            f"the music bed is {music.duration_seconds:.3f}s and the video is "
            f"{timeline.total_seconds:.3f}s; v1 does not loop a bed, so a short one would leave "
            f"silence under the tail"
        )


def as_json(timeline: Timeline) -> dict[str, Any]:
    """The authoring form as plain JSON — refs and all. What the digest hashes."""
    document: dict[str, Any] = {
        "version": timeline.version,
        "template": timeline.template,
        "width": timeline.width,
        "height": timeline.height,
        "fps": timeline.fps,
        "total_frames": timeline.total_frames,
        "clip_audio": timeline.clip_audio,
        "narration": {
            "ref": timeline.narration.ref_id,
            "mime_type": timeline.narration.mime_type,
            "duration_seconds": timeline.narration.duration_seconds,
        },
        "beats": [_beat_json(beat) for beat in timeline.beats],
    }
    if timeline.music is not None:
        document["music"] = {
            "ref": timeline.music.ref_id,
            "mime_type": timeline.music.mime_type,
            "duration_seconds": timeline.music.duration_seconds,
        }
    return document


def _beat_json(beat: Beat) -> dict[str, Any]:
    return {
        "index": beat.index,
        "start_frame": beat.start_frame,
        "frames": beat.frames,
        "pad_frames": beat.pad_frames,
        "clip": {
            "ref": beat.clip.ref_id,
            "mime_type": beat.clip.mime_type,
            "source_duration_seconds": beat.clip.source_duration_seconds,
            "in_seconds": beat.clip.in_seconds,
            "out_seconds": beat.clip.out_seconds,
        },
        "transition": {"kind": beat.transition.kind, "frames": beat.transition.frames},
        "captions": [
            {
                "text": caption.text,
                "start_frame": caption.start_frame,
                "end_frame": caption.end_frame,
            }
            for caption in beat.captions
        ],
    }


def timeline_from_json(document: Mapping[str, Any]) -> Timeline:
    """Rehydrate an authoring form — the other half of `as_json`.

    The final render (V4.2) reconstructs the approved timeline and recomputes
    its digest; a round trip that lost a field would make that check pass on a
    different video. `validate` is still the caller's to run: this reads a
    document, it does not bless one.
    """
    try:
        narration = document["narration"]
        beats_raw = document["beats"]
        timeline = Timeline(
            version=int(document.get("version", TIMELINE_VERSION)),
            template=str(document["template"]),  # type: ignore[arg-type]
            width=int(document.get("width", FRAME_WIDTH)),
            height=int(document.get("height", FRAME_HEIGHT)),
            fps=int(document.get("fps", FRAME_RATE)),
            clip_audio=str(document.get("clip_audio", "mute")),  # type: ignore[arg-type]
            narration=Narration(
                ref_id=str(narration["ref"]),
                mime_type=str(narration["mime_type"]),
                duration_seconds=float(narration["duration_seconds"]),
            ),
            beats=tuple(_beat_from_json(entry) for entry in beats_raw),
            music=_music_from_json(document.get("music")),
        )
    except TimelineError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineError(f"this is not a TimelineV1 document: {exc}") from exc
    return timeline


def _beat_from_json(entry: Mapping[str, Any]) -> Beat:
    clip = entry["clip"]
    transition = entry.get("transition") or {}
    return Beat(
        index=int(entry["index"]),
        start_frame=int(entry["start_frame"]),
        frames=int(entry["frames"]),
        pad_frames=int(entry.get("pad_frames", 0)),
        clip=Clip(
            ref_id=str(clip["ref"]),
            mime_type=str(clip["mime_type"]),
            source_duration_seconds=float(clip["source_duration_seconds"]),
            in_seconds=float(clip["in_seconds"]),
            out_seconds=float(clip["out_seconds"]),
        ),
        transition=Transition(
            kind=str(transition.get("kind", "none")),  # type: ignore[arg-type]
            frames=int(transition.get("frames", 0)),
        ),
        captions=tuple(
            Caption(
                text=str(caption["text"]),
                start_frame=int(caption["start_frame"]),
                end_frame=int(caption["end_frame"]),
            )
            for caption in entry.get("captions") or ()
        ),
    )


def _music_from_json(entry: Any) -> Music | None:
    if not entry:
        return None
    return Music(
        ref_id=str(entry["ref"]),
        mime_type=str(entry["mime_type"]),
        duration_seconds=float(entry["duration_seconds"]),
    )


def canonical_json(timeline: Timeline) -> str:
    """Byte-stable JSON: keys sorted, no insignificant whitespace, floats to six
    decimals, absent fields omitted. Stability across processes and Python
    versions is the only property the digest needs of it."""
    return json.dumps(
        _round_floats(as_json(timeline)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _round_floats(value: Any) -> Any:
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        # `+ 0.0` so a rounded -0.0 hashes the same as 0.0.
        return round(value, 6) + 0.0
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_floats(item) for item in value]
    return value


def timeline_digest(timeline: Timeline) -> str:
    """`sha256` of the canonical authoring form, hex.

    V4.3 records this beside the approval decision and the final render
    recomputes it, so approving one rough cut and rendering a different caption
    set is impossible. That is why every caption string is inside the hash.
    """
    return hashlib.sha256(canonical_json(timeline).encode("utf-8")).hexdigest()


def render_document(timeline: Timeline, paths: Mapping[str, str]) -> dict[str, Any]:
    """The authoring form with every ref replaced by its workdir-relative path.

    This is what becomes `timeline.json` in the sandbox. The child never learns
    a `BinaryRef` id, a workspace id or an original file name — and the mix
    constants ride along rather than being hard-coded in the renderer, so one
    document fully determines one render.
    """
    missing = [ref for ref in _refs(timeline) if ref not in paths]
    if missing:
        raise TimelineError(
            f"no materialized file for {len(missing)} timeline reference(s): "
            f"{', '.join(sorted(missing))}"
        )

    document = as_json(timeline)
    document["digest"] = timeline_digest(timeline)
    document["narration"] = {
        **{key: value for key, value in document["narration"].items() if key != "ref"},
        "file": paths[timeline.narration.ref_id],
        "target_lufs": NARRATION_TARGET_LUFS,
        "true_peak_ceiling_dbtp": NARRATION_TRUE_PEAK_DBTP,
    }
    if timeline.music is not None:
        document["music"] = {
            **{key: value for key, value in document["music"].items() if key != "ref"},
            "file": paths[timeline.music.ref_id],
            "target_lufs": MUSIC_TARGET_LUFS,
            "duck_db": MUSIC_DUCK_DB,
            "duck_attack_ms": MUSIC_DUCK_ATTACK_MS,
            "duck_release_ms": MUSIC_DUCK_RELEASE_MS,
        }
    document["clip_duck_db"] = CLIP_DUCK_DB
    document["caption_safe_area"] = dict(CAPTION_SAFE_AREA)
    for beat, source in zip(document["beats"], timeline.beats, strict=True):
        clip = {key: value for key, value in beat["clip"].items() if key != "ref"}
        clip["file"] = paths[source.clip.ref_id]
        beat["clip"] = clip
    return document


def _refs(timeline: Timeline) -> list[str]:
    refs = [timeline.narration.ref_id, *(beat.clip.ref_id for beat in timeline.beats)]
    if timeline.music is not None:
        refs.append(timeline.music.ref_id)
    return refs
