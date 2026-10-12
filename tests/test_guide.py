"""`guide-16x9`: the template profile, the overlay contract, its derivation from
a capture log, and `shortvideo.guide_assemble`.

The first group is the one that guards everything else: the guide is an
*additive* extension of `TimelineV1`, so a short's document, digest and render
document must be byte-for-byte what they were before it existed. The golden
values below were taken from the fixtures in `test_timeline.py` before the
template was added.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest
from tamtree_plugin_sdk import BinaryRef, Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.compose import ComposeNode
from tamtree_shortvideo.guide import (
    CalloutSpec,
    SceneLog,
    chapters_text,
    scene_overlay,
    split_caption,
    srt_text,
)
from tamtree_shortvideo.guide_assemble import NARRATION_PORT, GuideAssembleNode
from tamtree_shortvideo.timeline import (
    GUIDE_TEMPLATE,
    MAX_GUIDE_ZOOM,
    PROFILES,
    Beat,
    Callout,
    CameraKey,
    Caption,
    Clip,
    CursorPoint,
    GuideBeat,
    Narration,
    Timeline,
    TimelineError,
    TitleCard,
    as_json,
    render_document,
    timeline_digest,
    timeline_from_json,
    validate,
)
from tests.test_timeline import _paths, _timeline

FPS = 30

# -- a short is exactly what it was ------------------------------------------

GOLDEN = {
    "short-captioned": (
        "396ca4ef20bffa134a6d62ae6c16ebe68bbf655159f82f0f5a3e5824cb91ec8a",
        "db9f46229acf2efb01043a11e0ca4fac31369e597fd03433c63d045d933cfab5",
    ),
    "short-plain": (
        "50a2965559f4fe4143cf7d99cf7a914087f3be7e3c44d01aff2cb444c9961d4f",
        "fb46c762a7dee25d203c03a63326dcb5ff9d7ae29f457255763b25e0551bbfeb",
    ),
}


@pytest.mark.parametrize("template", sorted(GOLDEN))
def test_a_short_digests_and_renders_exactly_as_before_the_guide(template: str) -> None:
    timeline = _timeline(template=template)
    validate(timeline)
    render = json.dumps(
        render_document(timeline, _paths(timeline)), sort_keys=True, separators=(",", ":")
    )

    assert timeline_digest(timeline) == GOLDEN[template][0]
    assert hashlib.sha256(render.encode()).hexdigest() == GOLDEN[template][1]
    assert all("guide" not in beat for beat in as_json(timeline)["beats"])


def test_a_short_may_not_carry_a_guide_overlay() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(beats[0], guide=GuideBeat())
    with pytest.raises(TimelineError, match="only guide-16x9 does"):
        validate(replace(timeline, beats=tuple(beats)))


def test_a_short_keeps_its_own_ceilings() -> None:
    with pytest.raises(TimelineError, match="1080x1920 only"):
        validate(_timeline(width=1920, height=1080))
    with pytest.raises(TimelineError, match="no honest source"):
        validate(_timeline(beat_frames=(30 * 20,)))


# -- the guide timeline --------------------------------------------------------


def _guide_beat(
    index: int, *, start: int, frames: int, guide: GuideBeat | None = None, pad: int = 0
) -> Beat:
    played = (frames - pad) / FPS
    return Beat(
        index=index,
        start_frame=start,
        frames=frames,
        pad_frames=pad,
        clip=Clip(
            ref_id=f"bin_01GUIDE{index}REC",
            mime_type="video/mp4",
            source_duration_seconds=played + (0.0 if pad else 1.0),
            in_seconds=0.0,
            out_seconds=played,
        ),
        captions=(Caption(text=f"Section {index}.", start_frame=start, end_frame=start + 60),),
        guide=guide if guide is not None else GuideBeat(),
    )


def _guide(*beat_frames: int, guide: GuideBeat | None = None, pad: int = 0) -> Timeline:
    beats, start = [], 0
    for index, frames in enumerate(beat_frames or (900,)):
        beats.append(_guide_beat(index, start=start, frames=frames, guide=guide, pad=pad))
        start += frames
    return Timeline(
        template=GUIDE_TEMPLATE,
        width=1920,
        height=1080,
        narration=Narration(
            ref_id="bin_01GUIDENARR", mime_type="audio/wav", duration_seconds=start / FPS
        ),
        beats=tuple(beats),
    )


def test_a_guide_is_widescreen_and_its_sections_may_run_for_minutes() -> None:
    validate(_guide(30 * 120, 30 * 200))

    with pytest.raises(TimelineError, match="guide-16x9 renders 1920x1080 only"):
        validate(replace(_guide(), width=1080, height=1920))
    with pytest.raises(TimelineError, match="a guide section may run at most 300s"):
        validate(_guide(30 * 301))
    with pytest.raises(TimelineError, match="ceiling for guide-16x9 is 600.0s"):
        validate(_guide(*([30 * 250] * 3)))


def test_a_guide_may_hold_a_short_recording_for_seconds_not_forever() -> None:
    validate(_guide(900, pad=30 * 4))
    with pytest.raises(TimelineError, match="too short for the narration"):
        validate(_guide(900, pad=30 * 6))


def test_a_guide_caption_is_optional_and_may_be_longer() -> None:
    timeline = _guide()
    bare = replace(timeline.beats[0], captions=())
    validate(replace(timeline, beats=(bare,)))

    long = replace(
        timeline.beats[0], captions=(Caption(text="x" * 140, start_frame=0, end_frame=60),)
    )
    validate(replace(timeline, beats=(long,)))
    too_long = replace(long, captions=(Caption(text="x" * 141, start_frame=0, end_frame=60),))
    with pytest.raises(TimelineError, match="the ceiling is 140"):
        validate(replace(timeline, beats=(too_long,)))


def test_the_camera_may_not_upscale_or_look_past_the_recording() -> None:
    validate(_guide(guide=GuideBeat(camera=(CameraKey(0, 0.75, 0.25, MAX_GUIDE_ZOOM),))))

    with pytest.raises(TimelineError, match="the 2x recording would be upscaled"):
        validate(_guide(guide=GuideBeat(camera=(CameraKey(0, 0.5, 0.5, 2.5),))))
    with pytest.raises(TimelineError, match="past the edge of the recording"):
        validate(_guide(guide=GuideBeat(camera=(CameraKey(0, 0.9, 0.5, 2.0),))))
    with pytest.raises(TimelineError, match="increase strictly"):
        validate(
            _guide(guide=GuideBeat(camera=(CameraKey(10, 0.5, 0.5, 1), CameraKey(10, 0.5, 0.5, 1))))
        )


def test_overlay_frames_stay_inside_their_own_beat() -> None:
    with pytest.raises(TimelineError, match="outside its own beat"):
        validate(_guide(300, 300, guide=GuideBeat(cursor=(CursorPoint(310, 0.5, 0.5),))))


def test_a_callout_box_must_be_inside_the_recording_and_its_label_one_line() -> None:
    def callout(**changes: Any) -> Timeline:
        base = Callout(text="Open it", start_frame=0, end_frame=60, x=0.9, y=0.1, w=0.1, h=0.1)
        return _guide(guide=GuideBeat(callouts=(replace(base, **changes),)))

    validate(callout())
    with pytest.raises(TimelineError, match="right edge"):
        validate(callout(w=0.2))
    with pytest.raises(TimelineError, match="empty box"):
        validate(callout(h=0))
    with pytest.raises(TimelineError, match="control or surrogate"):
        validate(callout(text="two\nlines"))
    with pytest.raises(TimelineError, match="ends at or before its start"):
        validate(callout(end_frame=0))


def test_a_title_card_must_fit_its_beat() -> None:
    validate(_guide(90, guide=GuideBeat(title=TitleCard("Intro", 90))))
    with pytest.raises(TimelineError, match="must fit its beat"):
        validate(_guide(90, guide=GuideBeat(title=TitleCard("Intro", 91))))


def test_a_guide_round_trips_and_renders_with_its_own_caption_band() -> None:
    overlay = GuideBeat(
        title=TitleCard("Add a step", 75),
        camera=(CameraKey(0, 0.5, 0.5, 1.0), CameraKey(20, 0.6, 0.4, 1.8)),
        cursor=(CursorPoint(0, 0.5, 0.5), CursorPoint(30, 0.61, 0.42)),
        callouts=(Callout("Here", 30, 90, 0.55, 0.35, 0.1, 0.1),),
    )
    timeline = _guide(900, guide=overlay)

    again = timeline_from_json(json.loads(json.dumps(as_json(timeline))))
    assert again == timeline
    assert timeline_digest(again) == timeline_digest(timeline)

    document = render_document(timeline, _paths(timeline))
    assert document["caption_safe_area"] == dict(PROFILES[GUIDE_TEMPLATE].caption_safe_area)
    assert document["beats"][0]["guide"]["cursor"] == [[0, 0.5, 0.5], [30, 0.61, 0.42]]


# -- deriving the overlay from a capture log ----------------------------------

#: The shape `browser_capture.capture` emits for one scene: a 1920x1080 CSS
#: viewport recorded at 2x, a hover then a click on a 57px step handle.
SCENE: dict[str, Any] = {
    "scene_id": "open-step",
    "width": 3840,
    "height": 2160,
    "scale": 2,
    "duration_ms": 7000,
    "events": [
        {"step": 0, "action": "goto", "t0_ms": 0, "t1_ms": 400, "url": "/flows/x"},
        {
            "step": 1,
            "action": "click",
            "t0_ms": 2000,
            "t1_ms": 3000,
            "target": {"x": 843, "y": 376, "w": 57, "h": 57},
            "at": {"x": 872, "y": 405},
        },
        {"step": 2, "action": "pause", "t0_ms": 3000, "t1_ms": 5000},
    ],
    "cursor": [
        [0, 960, 540],
        [2000, 960, 540],
        [2400, 900, 450],
        [2800, 872, 405],
        [3000, 872, 405],
    ],
}


def _overlay(**overrides: Any) -> GuideBeat:
    args: dict[str, Any] = {
        "section": "open-step",
        "start_frame": 0,
        "frames": 210,
        "in_seconds": 0.0,
        "fps": FPS,
        **overrides,
    }
    return scene_overlay(SceneLog.from_capture(SCENE), **args)


def test_the_camera_eases_onto_the_target_ahead_of_the_pointer_and_back_out() -> None:
    camera = _overlay().camera

    assert camera[0] == CameraKey(0, 0.5, 0.5, 1.0)
    # Starts moving 0.3s before the step, arrives 0.7s later, framed on the handle.
    assert camera[1].frame == 51 and camera[1].zoom == 1.0
    assert camera[2].frame == 72
    assert camera[2].zoom == MAX_GUIDE_ZOOM
    assert camera[2].x == pytest.approx(871.5 / 1920, abs=1e-4)
    # Holds 1.6s past the step's end, then eases back to the whole screen.
    assert camera[-2].frame == 138 and camera[-2].zoom == MAX_GUIDE_ZOOM
    assert camera[-1] == CameraKey(159, 0.5, 0.5, 1.0)
    validate(_guide(210, guide=_overlay()))


def test_a_view_clamped_flush_to_an_edge_is_still_inside_it_after_rounding() -> None:
    # A 590x19 row near the bottom: zoom 1.7391..., centre clamped to the edge.
    # Rounded naively, the centre lands 5e-6 past it (found rendering the smoke
    # capture).
    scene = {
        **SCENE,
        "events": [
            {
                "step": 0,
                "action": "hover",
                "t0_ms": 500,
                "t1_ms": 1500,
                "target": {"x": 741, "y": 957, "w": 590, "h": 19},
                "at": {"x": 1036, "y": 967},
            }
        ],
    }
    overlay = scene_overlay(
        SceneLog.from_capture(scene), section="s", start_frame=0, frames=210, in_seconds=0, fps=FPS
    )
    validate(_guide(210, guide=overlay))
    assert max(key.y for key in overlay.camera) <= 1 - 0.5 / max(k.zoom for k in overlay.camera)


def test_the_cursor_is_one_point_per_frame_and_a_hold_is_its_two_ends() -> None:
    cursor = _overlay().cursor

    assert cursor[0] == CursorPoint(0, 0.5, 0.5)
    assert cursor[1] == CursorPoint(60, 0.5, 0.5)
    assert cursor[-1] == CursorPoint(90, round(872 / 1920, 4), round(405 / 1080, 4))
    assert len(cursor) == 5


def test_a_click_ripples_when_the_pointer_arrives_not_when_the_step_starts() -> None:
    (click,) = _overlay().clicks
    assert click.frame == 84  # the track reaches `at` at 2.8s


def test_a_callout_labels_its_step_from_the_press() -> None:
    (callout,) = _overlay(callouts=[CalloutSpec(step=1, text="Open the step")]).callouts

    assert (callout.start_frame, callout.end_frame) == (84, 159)
    assert callout.x == round(843 / 1920, 4)
    assert callout.w == pytest.approx(57 / 1920, abs=1e-4)


def test_a_callout_for_a_step_with_nothing_to_point_at_is_refused() -> None:
    with pytest.raises(TimelineError, match="step 2 .pause., which has no target box"):
        _overlay(callouts=[CalloutSpec(step=2, text="Wait")])
    with pytest.raises(TimelineError, match="step 9, which its capture did not record"):
        _overlay(callouts=[CalloutSpec(step=9, text="Nope")])


def test_log_times_land_on_the_timeline_clock_of_a_later_beat() -> None:
    overlay = _overlay(start_frame=300)
    assert overlay.camera[0].frame == 300
    assert overlay.clicks[0].frame == 384


def test_a_long_phrase_splits_at_words_into_proportionally_timed_captions() -> None:
    text = " ".join(["word"] * 60)  # 299 characters
    chunks = split_caption(text, start_seconds=1.0, end_seconds=4.0, limit=140)

    assert len(chunks) == 3
    assert all(len(chunk) <= 140 for chunk, _, _ in chunks)
    assert chunks[0][1] == 1.0 and chunks[-1][2] == 4.0
    assert chunks[0][2] == chunks[1][1]
    assert split_caption("short", start_seconds=0, end_seconds=1, limit=140) == [("short", 0, 1)]


def test_srt_and_chapters_come_from_the_timeline_frames() -> None:
    captions = [Caption("Hello.", 0, 45), Caption("Next.", 1830, 1900)]
    assert srt_text(captions, fps=FPS) == (
        "1\n00:00:00,000 --> 00:00:01,500\nHello.\n\n2\n00:01:01,000 --> 00:01:03,333\nNext.\n"
    )
    assert chapters_text([(0, "Intro"), (1830, "Add a step")], fps=FPS) == (
        "0:00 Intro\n1:01 Add a step\n"
    )


# -- the node -----------------------------------------------------------------

NARRATION_REF = BinaryRef(
    id="bin_01GUIDENARRATION",
    file_name="narration.wav",
    mime_type="audio/wav",
    size_bytes=1000,
    storage_key="ws/ws_test/binary/bin_01GUIDENARRATION",
)

SECTIONS = [
    {"id": "intro", "title": "What a digest is"},
    {
        "id": "open-step",
        "title": "Open a step",
        "callouts": [{"step": 1, "text": "Open the step"}],
    },
]


def _narration_item(phrases: list[tuple[str, str, float, float]], *, duration: float) -> Item:
    return Item.model_validate(
        {
            "json": {
                "sections": SECTIONS,
                "duration_seconds": duration,
                "captions": [
                    {"name": n, "text": t, "start_seconds": s, "end_seconds": e}
                    for n, t, s, e in phrases
                ],
                "audio": {"binary_property": "audio"},
            },
            "binary": {"audio": NARRATION_REF},
        }
    )


def _recording(scene_id: str, *, duration_ms: int) -> Item:
    ref = BinaryRef(
        id=f"bin_01REC{scene_id.upper().replace('-', '')}",
        file_name=f"{scene_id}.mp4",
        mime_type="video/mp4",
        size_bytes=1000,
        storage_key=f"ws/ws_test/binary/{scene_id}",
    )
    return Item.model_validate(
        {
            "json": {
                **SCENE,
                "scene_id": scene_id,
                "duration_ms": duration_ms,
                "attachment": "clip",
            },
            "binary": {"clip": ref},
        }
    )


PHRASES = [
    ("intro.1", "A digest collects the day's news into one email.", 0.3, 3.0),
    ("intro.2", "Let's build one.", 3.15, 4.5),
    ("open-step.1", "Click the step to open its panel.", 5.0, 9.0),
]


async def _assemble(
    phrases: list[tuple[str, str, float, float]] = PHRASES,
    *,
    duration: float = 10.0,
    recordings: dict[str, int] | None = None,
    **params: Any,
) -> tuple[FakeExecutionContext, Item]:
    recordings = recordings if recordings is not None else {"intro": 6000, "open-step": 7000}
    ctx = FakeExecutionContext(
        inputs={
            NARRATION_PORT: [_narration_item(phrases, duration=duration)],
            "main": [_recording(scene, duration_ms=ms) for scene, ms in recordings.items()],
        },
        params=params,
    )
    (out,) = (await GuideAssembleNode().execute(ctx))["main"]
    return ctx, out


async def test_one_beat_per_section_timed_by_its_first_phrase() -> None:
    ctx, out = await _assemble()
    timeline = timeline_from_json(out.json_["timeline"])
    validate(timeline)

    assert timeline.template == GUIDE_TEMPLATE
    assert [(b.start_frame, b.frames) for b in timeline.beats] == [(0, 150), (150, 150)]
    assert [b.pad_frames for b in timeline.beats] == [0, 0]
    assert timeline.beats[0].guide is not None and timeline.beats[0].guide.title is not None
    assert timeline.beats[0].guide.title.text == "What a digest is"
    assert [c.text for c in timeline.beats[1].guide.callouts] == ["Open the step"]  # type: ignore[union-attr]
    # Compose can find every artifact the timeline names on the item.
    wanted = {timeline.narration.ref_id, *(b.clip.ref_id for b in timeline.beats)}
    ctx._inputs["main"] = [out]
    found, _ = ComposeNode()._resolve_refs(ctx, wanted)
    assert set(found) == wanted


async def test_it_returns_srt_and_youtube_chapters() -> None:
    _, out = await _assemble()

    assert out.json_["chapters"] == "0:00 What a digest is\n0:05 Open a step\n"
    assert out.json_["srt"].startswith("1\n00:00:00,300 --> 00:00:03,000\nA digest collects")


async def test_captions_off_still_returns_the_srt() -> None:
    _, out = await _assemble(captions=False)
    timeline = timeline_from_json(out.json_["timeline"])

    assert all(not beat.captions for beat in timeline.beats)
    assert "Let's build one." in out.json_["srt"]


async def test_a_short_recording_holds_and_a_much_shorter_one_says_how_long_to_record() -> None:
    _, out = await _assemble(recordings={"intro": 3500, "open-step": 7000})
    assert out.json_["padded_sections"] == ["intro"]

    late = [*PHRASES[:2], ("open-step.1", "Click the step.", 8.0, 9.0)]
    with pytest.raises(NodeConfigurationError, match="min_duration_ms. to at least 8000"):
        await _assemble(late, recordings={"intro": 2000, "open-step": 7000})


async def test_a_phrase_that_names_no_section_is_refused() -> None:
    with pytest.raises(NodeConfigurationError, match="phrase 'outro.1' names no section"):
        await _assemble([*PHRASES, ("outro.1", "Bye.", 9.1, 9.5)])


async def test_sections_spoken_out_of_order_are_refused() -> None:
    swapped = [PHRASES[2], (PHRASES[0][0], PHRASES[0][1], 5.0, 8.0)]
    swapped[0] = (swapped[0][0], swapped[0][1], 0.0, 4.0)
    with pytest.raises(NodeConfigurationError, match="speaks the sections as open-step, intro"):
        await _assemble(swapped)


async def test_a_section_with_no_recording_is_refused_by_name() -> None:
    with pytest.raises(
        NodeConfigurationError, match="no recording arrived for section.s. open-step"
    ):
        await _assemble(recordings={"intro": 6000})
