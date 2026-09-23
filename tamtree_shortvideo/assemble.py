"""`shortvideo.assemble` — narration timing plus per-beat clips, as one `TimelineV1` (V4.1).

The join that the loop cannot do. `shortvideo.shot_list` numbered the beats,
the narration step timed them (one `beat-<N>` mark per beat), and the loop
generated and saved one clip per beat — each as a library asset, because a
parent holding twenty videos in memory until the last step is the thing
`generate-one-beat.yaml` exists to avoid. This node puts them back together
and hands `shortvideo.compose` **one item** carrying the timeline and every
artifact it names.

**Audio drives the timeline** (§3). Beat boundaries come from the marks via
`timeline.beat_boundaries` — the same function the freeze defines, so the
aggregation and the validator cannot derive frames two ways. A clip is trimmed
to its beat and never stretched; a clip up to half a second short holds its
last frame (`pad_frames`), and anything shorter is a refusal naming the beat,
because a video that freezes for a second is a defect a reviewer should not
have to spot.

**A missing beat is a refusal, not a shorter video.** The loop's
`on_item_error: skip` keeps the clips that succeeded and drops the ones that
did not; assembling the survivors would render a short whose narration talks
over footage from the wrong beat for the rest of its length. So every beat the
shot list promised must arrive, and the error names the ones that did not —
which is the input V4.4's "replay one failed beat" needs.

**Clip lengths are MiniMax's reported `duration`**, carried from
`minimax_collect`, not measured here — nothing in this node reads video bytes.
A clip a few frames shorter than reported is still safe: the renderer holds
the last decoded frame. A clip seconds shorter would be a provider defect the
draft render makes visible before anyone approves it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final

from tamtree_plugin_sdk import (
    BinaryRef,
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
)

from tamtree_shortvideo.shot_list import BEAT_MARK_PREFIX, beat_mark
from tamtree_shortvideo.timeline import (
    FRAME_RATE,
    MAX_PAD_SECONDS,
    MAX_TRANSITION_FRAMES,
    MAX_TRANSITION_SHARE,
    TEMPLATES,
    Beat,
    Caption,
    Clip,
    Narration,
    Timeline,
    TimelineError,
    Transition,
    as_json,
    beat_boundaries,
    timeline_digest,
    validate,
)

__all__ = ["NARRATION_PORT", "NODE_NAME", "AssembleNode", "clip_attachment"]

NODE_NAME: Final = "shortvideo.assemble"
NARRATION_PORT: Final = "narration"

#: Attachment name the narration rides under on the output item.
_NARRATION_ATTACHMENT: Final = "narration"
_DEFAULT_CROSSFADE: Final = 6
_EPSILON: Final = 1e-6


def clip_attachment(beat_number: int) -> str:
    """The attachment name one beat's clip rides under on the output item."""
    return f"clip-{beat_number:02d}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _beat_number(value: Any) -> int | None:
    number = _number(value)
    if number is None or number != int(number) or number < 1:
        return None
    return int(number)


def _plural(numbers: Sequence[int]) -> str:
    listed = ", ".join(str(n) for n in numbers)
    return f"beat {listed}" if len(numbers) == 1 else f"beats {listed}"


def _narration(ctx: ExecutionContext) -> Item:
    items = ctx.input_items(NARRATION_PORT)
    if len(items) != 1:
        raise NodeConfigurationError(
            f"Short video assemble needs exactly one narration item on its '{NARRATION_PORT}' "
            f"input, and got {len(items)}. Connect the narration step there."
        )
    return items[0]


def _narration_ref(item: Item) -> BinaryRef:
    binary = item.binary or {}
    audio = (item.json_ or {}).get("audio")
    wanted = audio.get("binary_property") if isinstance(audio, Mapping) else None
    if isinstance(wanted, str) and wanted in binary:
        return binary[wanted]
    audio_refs = [ref for ref in binary.values() if (ref.mime_type or "").startswith("audio/")]
    if len(audio_refs) == 1:
        return audio_refs[0]
    raise NodeConfigurationError(
        "Short video assemble: the narration item carries no audio attachment it can "
        "identify. Connect the narration step's output directly."
    )


def _expected_beats(narration: Mapping[str, Any], starts: Mapping[int, float]) -> list[int]:
    """The beat numbers the shot list promised — read off the narration item,
    which carries the shot list's `beats` through (the TTS nodes layer their
    output over the item they were given). Falls back to the marks when the
    list is absent, so a hand-built narration still assembles."""
    beats = narration.get("beats")
    if isinstance(beats, list) and beats:
        numbers = [
            _beat_number(entry.get("beat_number")) if isinstance(entry, Mapping) else None
            for entry in beats
        ]
        if any(number is None for number in numbers):
            raise NodeConfigurationError(
                "Short video assemble: the narration item's beat list has an entry without a "
                "whole `beat_number`."
            )
        return [n for n in numbers if n is not None]
    return sorted(starts)


def _mark_starts(narration: Mapping[str, Any]) -> tuple[dict[int, float], dict[int, str]]:
    """`beat_number -> start seconds` and `beat_number -> caption text`, from
    the narration's captions (or its bare marks when there are no captions)."""
    starts: dict[int, float] = {}
    texts: dict[int, str] = {}
    entries = narration.get("captions")
    if not isinstance(entries, list) or not entries:
        entries = narration.get("marks")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.startswith(BEAT_MARK_PREFIX):
            continue
        number = _beat_number(name[len(BEAT_MARK_PREFIX) :])
        seconds = _number(entry.get("start_seconds", entry.get("time_seconds")))
        if number is None or seconds is None:
            continue
        starts[number] = seconds
        text = entry.get("text")
        if isinstance(text, str):
            texts[number] = text
    return starts, texts


class AssembleNode(ProgrammaticNode):
    """Join the narration and every beat's clip into one renderable timeline."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — assemble timeline",
            "description": (
                "Join the narration and each beat's generated clip into one TimelineV1, timed "
                "by the narration's beat marks, ready for Short video — compose. Refuses, by "
                "beat number, when any beat has no clip."
            ),
            "category": "Files & media",
            "icon": "icons/shortvideo.svg",
            "kind": "action",
            "inputs": [{"name": "main"}, {"name": NARRATION_PORT}],
            "outputs": [
                {
                    "name": "main",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "timeline": {"type": "object"},
                            "digest": {"type": "string"},
                            "beats": {"type": "number"},
                            "frames": {"type": "number"},
                            "duration_seconds": {"type": "number"},
                            "padded_beats": {"type": "array", "items": {"type": "number"}},
                        },
                        "required": ["timeline", "digest", "beats", "frames"],
                    },
                }
            ],
            "params": [
                {
                    "name": "template",
                    "label": "Look",
                    "type": "options",
                    "default": "short-captioned",
                    "options": [
                        {"value": "short-captioned", "label": "Captioned — one caption per beat"},
                        {"value": "short-plain", "label": "Plain — no captions"},
                    ],
                    "description": "Which of the two first-party templates renders the short.",
                },
                {
                    "name": "crossfade_frames",
                    "label": "Crossfade",
                    "type": "number",
                    "default": _DEFAULT_CROSSFADE,
                    "description": (
                        f"Frames to dissolve between beats (30 per second), 0 for hard cuts, at "
                        f"most {MAX_TRANSITION_FRAMES}. Shortened automatically where a beat is "
                        "too short or a clip has no footage to spare."
                    ),
                },
            ],
        }
    )

    async def _clip_refs(
        self, ctx: ExecutionContext, expected: Sequence[int]
    ) -> dict[int, tuple[BinaryRef, float]]:
        """`beat_number -> (clip ref, source seconds)` for every expected beat,
        or a refusal naming the beats that have none."""
        by_beat: dict[int, Mapping[str, Any]] = {}
        duplicates: set[int] = set()
        for item in ctx.input_items():
            data = item.json_ or {}
            number = _beat_number(data.get("beat_number"))
            if number is None:
                raise NodeConfigurationError(
                    "Short video assemble: a clip arrived with no whole `beat_number`, so it "
                    "cannot be placed. Clips must come from the per-beat loop."
                )
            if number in by_beat:
                duplicates.add(number)
            by_beat[number] = data
        if duplicates:
            raise NodeConfigurationError(
                f"Short video assemble: {_plural(sorted(duplicates))} arrived more than once. "
                "Each beat must have exactly one clip."
            )
        missing = [n for n in expected if n not in by_beat]
        if missing:
            raise NodeConfigurationError(
                f"Short video assemble: {_plural(missing)} of {len(expected)} "
                f"{'has' if len(missing) == 1 else 'have'} no clip — "
                f"{'its' if len(missing) == 1 else 'their'} generation failed. "
                "The per-beat loop's status output says why. Re-run only those beats; the "
                "clips that succeeded are saved and are not generated again."
            )
        unexpected = sorted(set(by_beat) - set(expected))
        if unexpected:
            raise NodeConfigurationError(
                f"Short video assemble: {_plural(unexpected)} arrived but the narration has "
                "no such beat."
            )

        refs: dict[int, tuple[BinaryRef, float]] = {}
        for number in expected:
            clip = by_beat[number]
            seconds = _number(clip.get("duration_seconds"))
            if seconds is None or seconds <= 0:
                raise NodeConfigurationError(
                    f"Short video assemble: beat {number}'s clip has no duration, so it cannot "
                    "be trimmed to its narration."
                )
            asset_id = clip.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                raise NodeConfigurationError(
                    f"Short video assemble: beat {number}'s clip has no `asset_id`."
                )
            info = await ctx.assets.resolve(asset_id=asset_id)
            if info is None:
                raise NodeConfigurationError(
                    f"Short video assemble: beat {number}'s clip (asset {asset_id}) is not in "
                    "this workspace's library."
                )
            ref = BinaryRef(
                id=info.id,
                file_name=info.name,
                mime_type=info.mime_type,
                size_bytes=info.size_bytes,
                storage_key=info.content_storage_key,
            )
            refs[number] = (ref, seconds)
        return refs

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        narration_item = _narration(ctx)
        narration = narration_item.json_ or {}
        template = str(ctx.param("template") or "short-captioned")
        if template not in TEMPLATES:
            raise NodeConfigurationError(
                f"Short video assemble: {template!r} is not a template; use "
                f"{' or '.join(TEMPLATES)}."
            )
        crossfade = _number(ctx.param("crossfade_frames"))
        crossfade_frames = _DEFAULT_CROSSFADE if crossfade is None else int(crossfade)
        if not 0 <= crossfade_frames <= MAX_TRANSITION_FRAMES:
            raise NodeConfigurationError(
                f"Crossfade must be 0 to {MAX_TRANSITION_FRAMES} frames, not {crossfade_frames}."
            )

        duration = _number(narration.get("duration_seconds"))
        if duration is None or duration <= 0:
            raise NodeConfigurationError(
                "Short video assemble: the narration item has no measured `duration_seconds`."
            )
        starts, texts = _mark_starts(narration)
        expected = _expected_beats(narration, starts)
        unmarked = [n for n in expected if n not in starts]
        if unmarked:
            raise NodeConfigurationError(
                f"Short video assemble: the narration has no timing mark for {_plural(unmarked)} "
                f"(expected marks named {beat_mark(unmarked[0])!r}). Narrate the shot list's "
                "phrases in phrase-list mode so each beat is marked."
            )
        if template == "short-captioned":
            untexted = [n for n in expected if not texts.get(n, "").strip()]
            if untexted:
                raise NodeConfigurationError(
                    f"Short video assemble: {_plural(untexted)} "
                    f"{'has' if len(untexted) == 1 else 'have'} no caption text, and the "
                    "captioned look needs one per beat. Narrate in phrase-list mode, or choose "
                    "the plain look."
                )

        clips = await self._clip_refs(ctx, expected)
        fps = FRAME_RATE
        try:
            boundaries = beat_boundaries(
                [starts[n] for n in expected], duration_seconds=duration, fps=fps
            )
        except TimelineError as error:
            raise NodeConfigurationError(f"Short video assemble: {error}") from error

        beats: list[Beat] = []
        padded: list[int] = []
        max_pad = math.floor(MAX_PAD_SECONDS * fps + _EPSILON)
        for index, (number, (start_frame, frames)) in enumerate(
            zip(expected, boundaries, strict=True)
        ):
            ref, source = clips[number]
            playable = min(frames, math.floor(source * fps + _EPSILON))
            pad = frames - playable
            if pad > max_pad:
                raise NodeConfigurationError(
                    f"Short video assemble: beat {number}'s narration runs {frames / fps:.2f}s "
                    f"but its clip is only {source:.2f}s — more than {MAX_PAD_SECONDS}s short. "
                    "Shorten that line, split it into two beats, or generate longer clips."
                )
            if pad:
                padded.append(number)
            transition = Transition()
            if index and crossfade_frames:
                previous = beats[-1]
                tail = previous.clip.source_duration_seconds - previous.clip.out_seconds
                allowed = min(
                    crossfade_frames,
                    math.floor(MAX_TRANSITION_SHARE * frames + _EPSILON),
                    math.floor(MAX_TRANSITION_SHARE * previous.frames + _EPSILON),
                    math.floor(tail * fps + _EPSILON),
                )
                if allowed >= 1:
                    transition = Transition(kind="crossfade", frames=allowed)
            captions: tuple[Caption, ...] = ()
            if template == "short-captioned":
                captions = (
                    Caption(
                        text=texts[number].strip(),
                        start_frame=start_frame,
                        end_frame=start_frame + frames,
                    ),
                )
            beats.append(
                Beat(
                    index=index,
                    start_frame=start_frame,
                    frames=frames,
                    pad_frames=pad,
                    clip=Clip(
                        ref_id=ref.id,
                        mime_type=ref.mime_type or "video/mp4",
                        source_duration_seconds=source,
                        in_seconds=0.0,
                        out_seconds=playable / fps,
                    ),
                    transition=transition,
                    captions=captions,
                )
            )

        narration_ref = _narration_ref(narration_item)
        timeline = Timeline(
            template=template,  # type: ignore[arg-type]
            narration=Narration(
                ref_id=narration_ref.id,
                mime_type=narration_ref.mime_type or "audio/wav",
                duration_seconds=duration,
            ),
            beats=tuple(beats),
        )
        try:
            validate(timeline)
        except TimelineError as error:
            raise NodeConfigurationError(f"Short video assemble: {error}") from error

        binary: dict[str, BinaryRef] = {_NARRATION_ATTACHMENT: narration_ref}
        for number in expected:
            binary[clip_attachment(number)] = clips[number][0]
        return {
            "main": [
                Item.model_validate(
                    {
                        "json": {
                            "timeline": as_json(timeline),
                            "digest": timeline_digest(timeline),
                            "beats": len(beats),
                            "frames": timeline.total_frames,
                            "duration_seconds": round(timeline.total_seconds, 3),
                            "padded_beats": padded,
                        },
                        "binary": binary,
                    }
                )
            ]
        }
