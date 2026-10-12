"""The `guide-16x9` overlay, derived from one scene's capture log.

`browser_capture.capture` records a section of the real app and logs what it
did: each step's time window and target box, and the pointer's path. This
module turns that log into the `GuideBeat` a guide timeline carries — where the
camera looks, where the cursor is drawn, where a click ripples, which boxes are
called out, and the section's title card. It is pure: no I/O, no clock, so the
same log always yields the same overlay, and the same overlay the same digest.

**The camera follows the work, and only as far as the recording allows.** Each
step that points at something (`click`, `hover`, `fill`, a targeted `press`,
`scroll`) is a shot: the view eases onto the target's box, padded and kept
16:9, starting a beat before the pointer moves so the viewer is never chasing
it. A shot holds while the work stays there and eases back to the whole screen
after a pause, so a long explanation over a still screen is not spent staring
at one button. Zoom is capped at `MAX_GUIDE_ZOOM` because the capture records
at 2x — any tighter and the footage would be upscaled.

**Coordinates are fractions of the recording.** The log is in CSS pixels of
the capture viewport; dividing by that viewport is what lets the renderer place
the cursor and callouts at any output size, the half-size draft included.

**Times map onto the timeline's clock.** A log time `t` (ms since the clip's
first frame) lands at `start_frame + round((t/1000 - in_seconds) * fps)`; what
falls outside the beat is dropped, except the pointer's last position before
it, which is where the cursor starts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from tamtree_shortvideo.timeline import (
    MAX_GUIDE_ZOOM,
    Callout,
    CameraKey,
    Caption,
    Click,
    CursorPoint,
    GuideBeat,
    TimelineError,
    TitleCard,
)

__all__ = [
    "CalloutSpec",
    "SceneLog",
    "chapters_text",
    "scene_overlay",
    "split_caption",
    "srt_text",
]

#: How long the camera takes to ease onto a shot, and how far ahead of the
#: step it starts — so the view arrives about as the pointer does.
_MOVE_SECONDS: Final = 0.7
_LEAD_SECONDS: Final = 0.3
#: How long a shot holds after its step ends before the camera eases back to
#: the whole screen, if nothing else has claimed it by then.
_HOLD_SECONDS: Final = 1.6
#: A target is framed with this much room around it, and never tighter than
#: the frame `MAX_GUIDE_ZOOM` allows.
_PAD_FACTOR: Final = 1.6
_PAD_PX: Final = 160.0
#: A scroll moves what is under the camera, so it is framed loosely.
_SCROLL_MAX_ZOOM: Final = 1.25
#: Two shots this close are the same shot; moving between them is jitter.
_SAME_SHOT: Final = 0.03
_SAME_ZOOM: Final = 0.1
_DEFAULT_CALLOUT_SECONDS: Final = 2.5
_POINTED: Final = frozenset({"click", "hover", "fill", "press", "scroll"})
_PRESSED: Final = frozenset({"click", "fill"})
_DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class SceneLog:
    """The parts of one capture-log scene the overlay reads."""

    viewport_width: float
    viewport_height: float
    events: Sequence[Mapping[str, Any]]
    cursor: Sequence[Sequence[float]]

    @classmethod
    def from_capture(cls, item_json: Mapping[str, Any]) -> SceneLog:
        """From one `browser_capture.capture` output item. Its `width` and
        `height` are the recording's device pixels; the log is in CSS pixels,
        so the viewport is those divided by `scale`."""
        try:
            scale = float(item_json.get("scale") or 1)
            width = float(item_json["width"]) / scale
            height = float(item_json["height"]) / scale
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            raise TimelineError(
                f"capture item {item_json.get('scene_id')!r} has no usable recording size"
            ) from error
        if not (width > 0 and height > 0 and math.isfinite(width) and math.isfinite(height)):
            raise TimelineError(
                f"capture item {item_json.get('scene_id')!r} has no usable recording size"
            )
        events = item_json.get("events")
        cursor = item_json.get("cursor")
        return cls(
            viewport_width=width,
            viewport_height=height,
            events=[event for event in events if isinstance(event, Mapping)]
            if isinstance(events, list)
            else [],
            cursor=[point for point in cursor if _is_point(point)]
            if isinstance(cursor, list)
            else [],
        )


@dataclass(frozen=True, slots=True)
class CalloutSpec:
    """An author's callout: label step `step` of the scene with `text`."""

    step: int
    text: str
    seconds: float = _DEFAULT_CALLOUT_SECONDS


def _is_point(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and len(value) == 3
        and all(isinstance(part, int | float) and math.isfinite(part) for part in value)
    )


def _box(event: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    target = event.get("target")
    if not isinstance(target, Mapping):
        return None
    try:
        box = tuple(float(target[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in box) or box[2] <= 0 or box[3] <= 0:
        return None
    return box  # type: ignore[return-value]


def _seconds(event: Mapping[str, Any], key: str) -> float | None:
    value = event.get(key)
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value) / 1000
    return None


def _round(value: float) -> float:
    return round(value, _DIGITS) + 0.0


def _key(frame: int, view: tuple[float, float, float]) -> CameraKey:
    """A camera key, rounded without leaving the recording: zoom rounds down,
    and the centre is re-clamped to the rounded zoom and rounded inward — a
    view clamped flush to an edge must still be inside it after rounding."""
    scale = 10**_DIGITS
    zoom = max(1.0, math.floor(view[2] * scale) / scale)
    half = 0.5 / zoom
    low, high = math.ceil(half * scale) / scale, math.floor((1 - half) * scale) / scale
    x = min(max(_round(view[0]), low), high)
    y = min(max(_round(view[1]), low), high)
    return CameraKey(frame=frame, x=x + 0.0, y=y + 0.0, zoom=zoom)


class _Clock:
    """Log seconds ↔ timeline frames for one beat."""

    def __init__(self, *, start_frame: int, frames: int, in_seconds: float, fps: int) -> None:
        self.first = start_frame
        self.last = start_frame + frames
        self.in_seconds = in_seconds
        self.fps = fps

    def frame(self, seconds: float) -> int:
        return self.first + math.floor((seconds - self.in_seconds) * self.fps + 0.5)

    def clamp(self, frame: int) -> int:
        return max(self.first, min(self.last, frame))


def _shot(scene: SceneLog, event: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """Where the camera should look for this step, or None if it points at
    nothing. `(x, y, zoom)`: the view's centre as fractions, and its zoom."""
    action = event.get("action")
    if action == "goto":
        return (0.5, 0.5, 1.0)
    if action not in _POINTED:
        return None
    box = _box(event)
    if box is None:
        return None
    vw, vh = scene.viewport_width, scene.viewport_height
    x, y, w, h = box
    # The smallest 16:9-ish region holding the padded box.
    region_w = max(w * _PAD_FACTOR + _PAD_PX, (h * _PAD_FACTOR + _PAD_PX) * vw / vh)
    zoom = max(1.0, min(MAX_GUIDE_ZOOM, vw / region_w))
    if action == "scroll":
        zoom = min(zoom, _SCROLL_MAX_ZOOM)
    half = 0.5 / zoom
    cx = min(max((x + w / 2) / vw, half), 1 - half)
    cy = min(max((y + h / 2) / vh, half), 1 - half)
    return (cx, cy, zoom)


def _same(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return (
        abs(a[0] - b[0]) < _SAME_SHOT
        and abs(a[1] - b[1]) < _SAME_SHOT
        and abs(a[2] - b[2]) < _SAME_ZOOM
    )


def _camera(scene: SceneLog, clock: _Clock) -> tuple[CameraKey, ...]:
    wide = (0.5, 0.5, 1.0)
    # (seconds, view) pairs; consecutive pairs ease between their views.
    moves: list[tuple[float, tuple[float, float, float]]] = [(clock.in_seconds, wide)]
    current = wide
    free_at = clock.in_seconds  # when the camera is next free to move
    release: float | None = None  # when the current shot gives up and goes wide

    for event in scene.events:
        shot = _shot(scene, event)
        t0 = _seconds(event, "t0_ms")
        t1 = _seconds(event, "t1_ms")
        if shot is None or t0 is None:
            continue
        start = max(t0 - _LEAD_SECONDS, free_at)
        if release is not None and current != wide and release + _MOVE_SECONDS <= start:
            moves.append((release, current))
            moves.append((release + _MOVE_SECONDS, wide))
            current = wide
            free_at = release + _MOVE_SECONDS
            start = max(start, free_at)
        if not _same(shot, current):
            moves.append((start, current))
            moves.append((start + _MOVE_SECONDS, shot))
            current = shot
            free_at = start + _MOVE_SECONDS
        release = max(t1 if t1 is not None else t0, free_at) + _HOLD_SECONDS

    if release is not None and current != wide:
        moves.append((release, current))
        moves.append((release + _MOVE_SECONDS, wide))

    keys: list[CameraKey] = []
    for seconds, view in moves:
        frame = clock.frame(seconds)
        if frame > clock.last:
            break
        frame = clock.clamp(frame)
        key = _key(frame, view)
        if keys and frame <= keys[-1].frame:
            # Two keys in one frame: the later view is where the camera is.
            keys[-1] = CameraKey(frame=keys[-1].frame, x=key.x, y=key.y, zoom=key.zoom)
            continue
        keys.append(key)
    return tuple(keys)


def _cursor(scene: SceneLog, clock: _Clock) -> tuple[CursorPoint, ...]:
    """At most one point per frame, and a stationary run kept as its two ends
    — the renderer interpolates linearly, so the ends are all a hold needs."""
    vw, vh = scene.viewport_width, scene.viewport_height
    by_frame: dict[int, tuple[float, float]] = {}
    before: tuple[float, float] | None = None
    for t_ms, x, y in scene.cursor:
        position = (min(max(x / vw, 0.0), 1.0), min(max(y / vh, 0.0), 1.0))
        frame = clock.frame(t_ms / 1000)
        if frame < clock.first:
            before = position
            continue
        if frame > clock.last:
            break
        by_frame[frame] = position
    if before is not None and clock.first not in by_frame:
        by_frame[clock.first] = before

    frames = sorted(by_frame)
    points: list[CursorPoint] = []
    for index, frame in enumerate(frames):
        x, y = by_frame[frame]
        point = CursorPoint(frame=frame, x=_round(x), y=_round(y))
        if index and 0 < index < len(frames) - 1:
            previous = points[-1]
            following = by_frame[frames[index + 1]]
            # The middle of a run that neither moved in nor moves out.
            if (previous.x, previous.y) == (point.x, point.y) and (
                _round(following[0]),
                _round(following[1]),
            ) == (point.x, point.y):
                continue
        points.append(point)
    return tuple(points)


def _press_seconds(scene: SceneLog, event: Mapping[str, Any]) -> float | None:
    """When the pointer arrives on a pressed target: the first track point of
    the step that sits on its `at`. The press follows the arrival directly."""
    t0 = _seconds(event, "t0_ms")
    t1 = _seconds(event, "t1_ms")
    at = event.get("at")
    if t0 is None or not isinstance(at, Mapping):
        return None
    try:
        ax, ay = float(at["x"]), float(at["y"])
    except (KeyError, TypeError, ValueError):
        return None
    for t_ms, x, y in scene.cursor:
        seconds = t_ms / 1000
        if seconds < t0 - 1e-3:
            continue
        if t1 is not None and seconds > t1 + 1e-3:
            break
        if abs(x - ax) <= 0.5 and abs(y - ay) <= 0.5:
            return seconds
    return t1 if t1 is not None else t0


def _clicks(scene: SceneLog, clock: _Clock) -> tuple[Click, ...]:
    vw, vh = scene.viewport_width, scene.viewport_height
    clicks: list[Click] = []
    for event in scene.events:
        if event.get("action") not in _PRESSED:
            continue
        seconds = _press_seconds(scene, event)
        at = event.get("at")
        if seconds is None or not isinstance(at, Mapping):
            continue
        frame = clock.frame(seconds)
        if not clock.first <= frame <= clock.last:
            continue
        clicks.append(
            Click(
                frame=frame,
                x=_round(min(max(float(at["x"]) / vw, 0.0), 1.0)),
                y=_round(min(max(float(at["y"]) / vh, 0.0), 1.0)),
            )
        )
    return tuple(clicks)


def _callouts(
    scene: SceneLog, clock: _Clock, specs: Sequence[CalloutSpec], *, section: str
) -> tuple[Callout, ...]:
    vw, vh = scene.viewport_width, scene.viewport_height
    by_step = {event.get("step"): event for event in scene.events}
    callouts: list[Callout] = []
    for spec in specs:
        event = by_step.get(spec.step)
        if event is None:
            raise TimelineError(
                f"section {section!r} calls out step {spec.step}, which its capture did not record"
            )
        box = _box(event)
        if box is None:
            raise TimelineError(
                f"section {section!r} calls out step {spec.step} ({event.get('action')}), which "
                f"has no target box to point at"
            )
        seconds = _press_seconds(scene, event) if event.get("action") in _PRESSED else None
        if seconds is None:
            seconds = _seconds(event, "t0_ms") or 0.0
        start = clock.frame(seconds)
        end = clock.frame(seconds + max(spec.seconds, 0.0))
        start, end = clock.clamp(start), clock.clamp(end)
        if end <= start:
            # Off the end of the beat: the recording ran past the narration.
            continue
        x, y, w, h = box
        left, top = max(x / vw, 0.0), max(y / vh, 0.0)
        right, bottom = min((x + w) / vw, 1.0), min((y + h) / vh, 1.0)
        if right <= left or bottom <= top:
            continue
        callouts.append(
            Callout(
                text=spec.text,
                start_frame=start,
                end_frame=end,
                x=_round(left),
                y=_round(top),
                # Rounded down so a box flush with the edge stays inside it.
                w=math.floor((right - left) * 10**_DIGITS) / 10**_DIGITS,
                h=math.floor((bottom - top) * 10**_DIGITS) / 10**_DIGITS,
            )
        )
    return tuple(callouts)


def scene_overlay(
    scene: SceneLog,
    *,
    section: str,
    start_frame: int,
    frames: int,
    in_seconds: float,
    fps: int,
    title: str | None = None,
    title_frames: int = 0,
    callouts: Sequence[CalloutSpec] = (),
) -> GuideBeat:
    """One beat's `GuideBeat`, from its scene's log."""
    clock = _Clock(start_frame=start_frame, frames=frames, in_seconds=in_seconds, fps=fps)
    card = None
    if title and title.strip():
        card = TitleCard(text=title.strip(), frames=max(1, min(title_frames, frames)))
    return GuideBeat(
        title=card,
        camera=_camera(scene, clock),
        cursor=_cursor(scene, clock),
        clicks=_clicks(scene, clock),
        callouts=_callouts(scene, clock, callouts, section=section),
    )


def split_caption(
    text: str, *, start_seconds: float, end_seconds: float, limit: int
) -> list[tuple[str, float, float]]:
    """A phrase too long for one caption, as consecutive chunks of at most
    `limit` characters, broken at words, each timed in proportion to its
    length. A phrase that fits comes back whole."""
    words = text.split()
    if len(text) <= limit or len(words) < 2:
        return [(text, start_seconds, end_seconds)]
    chunks: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}" if line else word
        if len(candidate) > limit and line:
            chunks.append(line)
            line = word
        else:
            line = candidate
    chunks.append(line)
    total = sum(len(chunk) for chunk in chunks)
    span = end_seconds - start_seconds
    timed: list[tuple[str, float, float]] = []
    at = start_seconds
    for index, chunk in enumerate(chunks):
        end = end_seconds if index == len(chunks) - 1 else at + span * len(chunk) / total
        timed.append((chunk, at, end))
        at = end
    return timed


def _timestamp(seconds: float, *, srt: bool) -> str:
    millis = max(0, math.floor(seconds * 1000 + 0.5))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def srt_text(captions: Sequence[Caption], *, fps: int) -> str:
    """SubRip, from the timeline's own caption frames — so the burned-in and
    uploaded captions cannot disagree."""
    blocks = []
    for number, caption in enumerate(captions, start=1):
        start = _timestamp(caption.start_frame / fps, srt=True)
        end = _timestamp(caption.end_frame / fps, srt=True)
        blocks.append(f"{number}\n{start} --> {end}\n{caption.text}\n")
    return "\n".join(blocks)


def chapters_text(chapters: Sequence[tuple[int, str]], *, fps: int) -> str:
    """YouTube's chapter list: `M:SS Title` per line, the first at 0:00."""
    return "\n".join(
        f"{_timestamp(start_frame / fps, srt=False)} {title}" for start_frame, title in chapters
    ) + ("\n" if chapters else "")
