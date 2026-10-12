"""`shortvideo.guide_assemble` — a guide's narration and recordings, as one `guide-16x9` timeline.

The guide's counterpart of `shortvideo.assemble`. A guide is a list of
sections; each is narrated by one or more phrases and recorded by one
`browser_capture.capture` scene. This node joins them: **one beat per
section**, timed by the narration, showing that section's recording with the
overlay `guide.scene_overlay` derives from its capture log.

**Phrases name their section.** The narration step runs in captions mode over
every phrase of the guide, each named `<section id>.<anything>` (or just the
section id), so one synthesis yields one track and a timing mark per phrase. A
section starts where its first phrase does; beat 0 still starts at frame 0,
absorbing any lead-in. A phrase that names no section, a section with no
phrase, and sections spoken out of order are all refusals that name the
offender, because each would put narration over the wrong recording.

**The recording is paced to the narration, not trimmed to it.** The capture
scene's `min_duration_ms` should be at least its section's spoken length. A
recording that runs long is cut at the beat; one that runs short holds its
last frame — a still screen, which is honest — up to the template's pad
ceiling, past which the refusal says how much longer the scene must run.

**Sections come from the narration item, or from the param.** The step that
wrote the guide puts `sections` on its items and the narration step layers its
output over them, so by default this node reads them there. Each section is
`{"id", "title"?, "callouts"?: [{"step", "text", "seconds"?}]}`.
"""

from __future__ import annotations

import json
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

from tamtree_shortvideo.guide import (
    CalloutSpec,
    SceneLog,
    chapters_text,
    scene_overlay,
    split_caption,
    srt_text,
)
from tamtree_shortvideo.timeline import (
    FRAME_RATE,
    GUIDE_TEMPLATE,
    MAX_TRANSITION_FRAMES,
    PROFILES,
    SUPPORTED_VIDEO_TYPES,
    Beat,
    Caption,
    Clip,
    Narration,
    Timeline,
    TimelineError,
    Transition,
    as_json,
    beat_boundaries,
    frames_at,
    timeline_digest,
    validate,
)

__all__ = ["NARRATION_PORT", "NODE_NAME", "GuideAssembleNode", "section_attachment"]

NODE_NAME: Final = "shortvideo.guide_assemble"
NARRATION_PORT: Final = "narration"

_NARRATION_ATTACHMENT: Final = "narration"
_DEFAULT_TITLE_SECONDS: Final = 2.5
_EPSILON: Final = 1e-6


def section_attachment(index: int) -> str:
    """The attachment name one section's recording rides under on the output."""
    return f"section-{index + 1:02d}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _refuse(message: str) -> NodeConfigurationError:
    return NodeConfigurationError(f"Guide assemble: {message}")


def _narration(ctx: ExecutionContext) -> Item:
    items = ctx.input_items(NARRATION_PORT)
    if len(items) != 1:
        raise _refuse(
            f"needs exactly one narration item on its '{NARRATION_PORT}' input, and got "
            f"{len(items)}. Run the narration step once over every phrase of the guide, in "
            "captions mode, and connect it there."
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
    raise _refuse(
        "the narration item carries no audio attachment it can identify. Connect the narration "
        "step's output directly."
    )


def _sections(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise _refuse(f"Sections is not valid JSON — {error}") from None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _refuse(f"Sections must be a list, and this is a {type(raw).__name__}.")
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise _refuse(f"section {position + 1} is not an object.")
        section_id = entry.get("id")
        if not isinstance(section_id, str) or not section_id:
            raise _refuse(f"section {position + 1} has no `id`.")
        if section_id in seen:
            raise _refuse(f"section id {section_id!r} is used twice.")
        seen.add(section_id)
        title = entry.get("title")
        if title is not None and not isinstance(title, str):
            raise _refuse(f"section {section_id!r} has a `title` that is not text.")
        sections.append(
            {"id": section_id, "title": title, "callouts": _callouts(entry, section_id)}
        )
    return sections


def _callouts(entry: Mapping[str, Any], section_id: str) -> list[CalloutSpec]:
    raw = entry.get("callouts") or []
    if not isinstance(raw, list):
        raise _refuse(f"section {section_id!r} has `callouts` that are not a list.")
    specs: list[CalloutSpec] = []
    for position, callout in enumerate(raw):
        where = f"section {section_id!r}, callout {position + 1}"
        if not isinstance(callout, Mapping):
            raise _refuse(f"{where} is not an object.")
        step = _number(callout.get("step"))
        text = callout.get("text")
        if step is None or step != int(step) or step < 0:
            raise _refuse(f"{where} needs a whole `step` number (the scene step it labels).")
        if not isinstance(text, str) or not text.strip():
            raise _refuse(f"{where} has no `text`.")
        seconds = _number(callout.get("seconds"))
        specs.append(
            CalloutSpec(step=int(step), text=text.strip(), seconds=seconds)
            if seconds is not None and seconds > 0
            else CalloutSpec(step=int(step), text=text.strip())
        )
    return specs


def _section_of(name: str, ids: Sequence[str]) -> str | None:
    for section_id in ids:
        if name == section_id or name.startswith(f"{section_id}."):
            return section_id
    return None


def _phrases(narration: Mapping[str, Any]) -> list[tuple[str, str, float, float]]:
    """`(name, text, start, end)` per narrated phrase, in order."""
    entries = narration.get("captions")
    phrases: list[tuple[str, str, float, float]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name, text = entry.get("name"), entry.get("text")
        start, end = _number(entry.get("start_seconds")), _number(entry.get("end_seconds"))
        if not isinstance(name, str) or start is None or end is None:
            continue
        phrases.append((name, text if isinstance(text, str) else "", start, end))
    return phrases


def _scene_items(ctx: ExecutionContext) -> dict[str, Item]:
    scenes: dict[str, Item] = {}
    for item in ctx.input_items():
        scene_id = (item.json_ or {}).get("scene_id")
        if isinstance(scene_id, str) and scene_id:
            scenes.setdefault(scene_id, item)
    return scenes


def _clip_ref(item: Item, section_id: str) -> BinaryRef:
    binary = item.binary or {}
    wanted = (item.json_ or {}).get("attachment")
    ref = binary.get(wanted) if isinstance(wanted, str) else None
    if ref is None:
        videos = [r for r in binary.values() if (r.mime_type or "").startswith("video/")]
        ref = videos[0] if len(videos) == 1 else None
    if ref is None:
        raise _refuse(f"the recording for section {section_id!r} carries no video attachment.")
    if ref.mime_type not in SUPPORTED_VIDEO_TYPES:
        raise _refuse(
            f"the recording for section {section_id!r} is {ref.mime_type!r}; composition accepts "
            f"{', '.join(SUPPORTED_VIDEO_TYPES)}."
        )
    return ref


class GuideAssembleNode(ProgrammaticNode):
    """Join a guide's narration and its per-section recordings into one timeline."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — assemble guide",
            "description": (
                "Join a guide's narration and one screen recording per section into a 16:9 "
                "timeline — zoom and pan, cursor, clicks, callouts, section titles and captions "
                "from each recording's capture log — ready for Short video — compose. Also "
                "returns the captions as SRT and a YouTube chapter list."
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
                            "padded_sections": {"type": "array", "items": {"type": "string"}},
                            "srt": {"type": "string"},
                            "chapters": {"type": "string"},
                        },
                        "required": ["timeline", "digest", "beats", "frames", "srt", "chapters"],
                    },
                }
            ],
            "params": [
                {
                    "name": "sections",
                    "label": "Sections",
                    "type": "json",
                    "default": "",
                    "description": (
                        'The guide\'s sections in order: [{"id", "title", "callouts": [{"step", '
                        '"text"}]}]. Each id is a capture scene id, and the phrases narrating '
                        "it are named `<id>.<n>`. Leave blank to read `sections` from the "
                        "narration item."
                    ),
                },
                {
                    "name": "captions",
                    "label": "Burn in captions",
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Draw the narration as captions. The SRT is returned either way, for "
                        "the destination's own subtitle track."
                    ),
                },
                {
                    "name": "title_seconds",
                    "label": "Section title",
                    "type": "number",
                    "default": _DEFAULT_TITLE_SECONDS,
                    "description": (
                        "Seconds each section's title shows over the start of its recording; "
                        "0 for none."
                    ),
                },
                {
                    "name": "crossfade_frames",
                    "label": "Crossfade",
                    "type": "number",
                    "default": 0,
                    "description": (
                        f"Frames to dissolve between sections (30 per second), 0 for cuts, at "
                        f"most {MAX_TRANSITION_FRAMES}. Dropped where a recording has no footage "
                        "to spare past its section."
                    ),
                },
            ],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        shape = PROFILES[GUIDE_TEMPLATE]
        fps = FRAME_RATE
        narration_item = _narration(ctx)
        narration = narration_item.json_ or {}

        sections = _sections(ctx.param("sections"))
        if not sections:
            sections = _sections(narration.get("sections"))
        if not sections:
            raise _refuse(
                "there are no sections. Give the Sections parameter, or carry `sections` on the "
                "narration item."
            )
        ids = [section["id"] for section in sections]

        duration = _number(narration.get("duration_seconds"))
        if duration is None or duration <= 0:
            raise _refuse("the narration item has no measured `duration_seconds`.")

        # Phrase → section, in spoken order.
        phrases = _phrases(narration)
        if not phrases:
            raise _refuse(
                "the narration carries no phrase timings. Run the narration step in captions "
                "mode, with one named phrase per line."
            )
        starts: dict[str, float] = {}
        by_section: dict[str, list[tuple[str, float, float]]] = {i: [] for i in ids}
        order: list[str] = []
        for name, text, start, end in phrases:
            section_id = _section_of(name, ids)
            if section_id is None:
                raise _refuse(
                    f"phrase {name!r} names no section. Name each phrase `<section id>.<n>`; "
                    f"the sections are {', '.join(ids)}."
                )
            if not order or order[-1] != section_id:
                if section_id in starts:
                    raise _refuse(
                        f"section {section_id!r} is narrated in two places — its phrases must "
                        "be consecutive."
                    )
                order.append(section_id)
                starts[section_id] = start
            by_section[section_id].append((text, start, end))
        unspoken = [i for i in ids if i not in starts]
        if unspoken:
            raise _refuse(f"no phrase narrates section(s) {', '.join(unspoken)}.")
        if order != ids:
            raise _refuse(
                f"the narration speaks the sections as {', '.join(order)}, but they are listed "
                f"as {', '.join(ids)}."
            )

        try:
            boundaries = beat_boundaries(
                [starts[i] for i in ids], duration_seconds=duration, fps=fps
            )
        except TimelineError as error:
            raise _refuse(str(error)) from error

        scenes = _scene_items(ctx)
        missing = [i for i in ids if i not in scenes]
        if missing:
            raise _refuse(
                f"no recording arrived for section(s) {', '.join(missing)}. Each section id must "
                "be a capture scene id."
            )

        burn = ctx.param("captions")
        burn_captions = True if burn is None else bool(burn)
        title_seconds = _number(ctx.param("title_seconds"))
        title_frames = frames_at(
            _DEFAULT_TITLE_SECONDS if title_seconds is None else max(title_seconds, 0.0), fps
        )
        crossfade = _number(ctx.param("crossfade_frames"))
        crossfade_frames = 0 if crossfade is None else int(crossfade)
        if not 0 <= crossfade_frames <= MAX_TRANSITION_FRAMES:
            raise _refuse(
                f"Crossfade must be 0 to {MAX_TRANSITION_FRAMES} frames, not {crossfade_frames}."
            )

        beats: list[Beat] = []
        binary: dict[str, BinaryRef] = {_NARRATION_ATTACHMENT: _narration_ref(narration_item)}
        all_captions: list[Caption] = []
        padded: list[str] = []
        max_pad = math.floor(shape.max_pad_seconds * fps + _EPSILON)
        for index, (section, (start_frame, frames)) in enumerate(
            zip(sections, boundaries, strict=True)
        ):
            section_id = section["id"]
            item = scenes[section_id]
            item_json = item.json_ or {}
            ref = _clip_ref(item, section_id)
            source = _number(item_json.get("duration_ms"))
            if source is None or source <= 0:
                raise _refuse(f"the recording for section {section_id!r} has no `duration_ms`.")
            source /= 1000

            beat_seconds = frames / fps
            if source + _EPSILON >= beat_seconds:
                pad = 0
            else:
                pad = frames - math.floor(source * fps + _EPSILON)
                if pad > max_pad:
                    raise _refuse(
                        f"section {section_id!r} is narrated for {beat_seconds:.2f}s but its "
                        f"recording is only {source:.2f}s — more than {shape.max_pad_seconds:g}s "
                        f"short. Set the scene's `min_duration_ms` to at least "
                        f"{math.ceil(beat_seconds * 1000)}."
                    )
                padded.append(section_id)
            out_seconds = (frames - pad) / fps

            transition = Transition()
            if index > 0 and crossfade_frames:
                previous = beats[-1]
                tail = previous.clip.source_duration_seconds - previous.clip.out_seconds
                share = math.floor(0.25 * min(frames, previous.frames))
                usable = min(crossfade_frames, share, math.floor(tail * fps + _EPSILON))
                if usable >= 1:
                    transition = Transition(kind="crossfade", frames=usable)

            # Built whether or not they are burned in: the SRT is made from them.
            captions: list[Caption] = []
            end_of_beat = start_frame + frames
            previous_end = start_frame
            for text, phrase_start, phrase_end in by_section[section_id]:
                if not text.strip():
                    continue
                for chunk, chunk_start, chunk_end in split_caption(
                    " ".join(text.split()),
                    start_seconds=phrase_start,
                    end_seconds=phrase_end,
                    limit=shape.max_caption_characters,
                ):
                    first = max(frames_at(chunk_start, fps), previous_end)
                    last = min(frames_at(chunk_end, fps), end_of_beat)
                    if last <= first:
                        continue
                    captions.append(Caption(text=chunk, start_frame=first, end_frame=last))
                    previous_end = last
            all_captions.extend(captions)

            try:
                overlay = scene_overlay(
                    SceneLog.from_capture(item_json),
                    section=section_id,
                    start_frame=start_frame,
                    frames=frames,
                    in_seconds=0.0,
                    fps=fps,
                    title=section["title"] if title_frames else None,
                    title_frames=title_frames,
                    callouts=section["callouts"],
                )
            except TimelineError as error:
                raise _refuse(str(error)) from error

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
                        out_seconds=out_seconds,
                    ),
                    transition=transition,
                    captions=tuple(captions) if burn_captions else (),
                    guide=overlay,
                )
            )
            binary[section_attachment(index)] = ref

        narration_ref = binary[_NARRATION_ATTACHMENT]
        timeline = Timeline(
            template=GUIDE_TEMPLATE,
            width=shape.width,
            height=shape.height,
            fps=fps,
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
            raise _refuse(str(error)) from error

        chapters = [
            (beat.start_frame, (section["title"] or section["id"]).strip())
            for beat, section in zip(beats, sections, strict=True)
        ]
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
                            "padded_sections": padded,
                            "srt": srt_text(all_captions, fps=fps),
                            "chapters": chapters_text(chapters, fps=fps),
                        },
                        "binary": binary,
                    }
                )
            ]
        }
