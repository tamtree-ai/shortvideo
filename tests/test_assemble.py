"""V4.1: narration timing and per-beat clips become one renderable `TimelineV1`.

The fixtures are the real shapes: the narration item is what
`shortvideo.google_tts` emits in phrase-list mode over the shot list's item
(its `beats` still on it, one `beat-<N>` caption per beat), and each clip item
is what `generate-one-beat.yaml`'s `label_clip` hands back — a beat number, a
reported duration and a library `asset_id`, no bytes.

The assertion that matters most is the last group: a missing beat is refused
by number rather than rendered around.
"""

from __future__ import annotations

from typing import Any

import pytest
from tamtree_plugin_sdk import BinaryRef, Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.assemble import NARRATION_PORT, AssembleNode, clip_attachment
from tamtree_shortvideo.compose import ComposeNode
from tamtree_shortvideo.shot_list import beat_mark
from tamtree_shortvideo.timeline import timeline_digest, timeline_from_json, validate

FPS = 30
NARRATION_REF = BinaryRef(
    id="bin_01NARRATION",
    file_name="narrate.wav",
    mime_type="audio/wav",
    size_bytes=1000,
    storage_key="ws/ws_test/binary/bin_01NARRATION",
)


def _narration(
    starts: list[float], *, duration: float, texts: list[str] | None = None, **extra: Any
) -> Item:
    texts = texts or [f"Line {n}." for n in range(1, len(starts) + 1)]
    captions = [
        {
            "name": beat_mark(n),
            "text": texts[n - 1],
            "start_seconds": start,
            "end_seconds": starts[n] if n < len(starts) else duration,
        }
        for n, start in enumerate(starts, start=1)
    ]
    json_: dict[str, Any] = {
        "beats": [
            {"beat_number": n, "narration_text": texts[n - 1], "visual_prompt": "x"}
            for n in range(1, len(starts) + 1)
        ],
        "duration_seconds": duration,
        "marks": [{"name": c["name"], "time_seconds": c["start_seconds"]} for c in captions],
        "captions": captions,
        "audio": {"binary_property": "audio", "encoding": "LINEAR16", "size_bytes": 1000},
        **extra,
    }
    return Item.model_validate({"json": json_, "binary": {"audio": NARRATION_REF}})


async def _clips(
    ctx: FakeExecutionContext, seconds: dict[int, float], *, skip: tuple[int, ...] = ()
) -> list[Item]:
    items = []
    for number, length in seconds.items():
        info = await ctx.assets.save(
            data=f"clip {number}".encode(), name=f"beat-{number}", mime_type="video/mp4"
        )
        if number in skip:
            continue
        items.append(
            Item.model_validate(
                {
                    "json": {
                        "beat_number": number,
                        "asset_id": info.id,
                        "duration_seconds": length,
                        "task_id": f"task-{number}",
                    }
                }
            )
        )
    return items


async def _assemble(
    narration: Item,
    clips: dict[int, float],
    *,
    skip: tuple[int, ...] = (),
    extra_clips: list[Item] | None = None,
    **params: Any,
) -> Item:
    ctx = FakeExecutionContext(inputs={NARRATION_PORT: [narration]}, params=params)
    main = await _clips(ctx, clips, skip=skip)
    ctx._inputs["main"] = main + (extra_clips or [])
    (out,) = (await AssembleNode().execute(ctx))["main"]
    return out


# --- the happy path ----------------------------------------------------------


async def test_three_beats_become_a_valid_timeline_timed_by_the_marks() -> None:
    out = await _assemble(_narration([0.2, 5.0, 10.5], duration=15.0), {1: 6.0, 2: 6.0, 3: 6.0})
    timeline = timeline_from_json(out.json_["timeline"])

    validate(timeline)
    # Beat 1 starts at frame 0 regardless of its mark; the rest at their marks.
    assert [(b.start_frame, b.frames) for b in timeline.beats] == [(0, 150), (150, 165), (315, 135)]
    assert timeline.total_frames == 450 == out.json_["frames"]
    assert out.json_["digest"] == timeline_digest(timeline)


async def test_each_clip_is_trimmed_to_its_beat_and_never_stretched() -> None:
    out = await _assemble(_narration([0.0, 4.0], duration=9.0), {1: 6.0, 2: 6.0})
    beats = timeline_from_json(out.json_["timeline"]).beats

    assert (beats[0].clip.in_seconds, beats[0].clip.out_seconds) == (0.0, 4.0)
    assert beats[1].clip.out_seconds == pytest.approx(5.0)
    assert all(b.pad_frames == 0 for b in beats)


async def test_a_clip_slightly_short_holds_its_last_frame_and_says_so() -> None:
    out = await _assemble(_narration([0.0, 6.0], duration=12.3), {1: 6.0, 2: 6.0})
    last = timeline_from_json(out.json_["timeline"]).beats[1]

    assert last.pad_frames == 9  # 6.3s of narration over 6.0s of footage
    assert out.json_["padded_beats"] == [2]


async def test_captions_are_the_beat_s_own_line_spanning_the_beat() -> None:
    out = await _assemble(
        _narration([0.0, 4.0], duration=8.0, texts=["First.", "Second."]), {1: 6.0, 2: 6.0}
    )
    beats = timeline_from_json(out.json_["timeline"]).beats

    assert [(c.text, c.start_frame, c.end_frame) for b in beats for c in b.captions] == [
        ("First.", 0, 120),
        ("Second.", 120, 240),
    ]


async def test_the_plain_look_carries_no_captions() -> None:
    out = await _assemble(
        _narration([0.0, 4.0], duration=8.0), {1: 6.0, 2: 6.0}, template="short-plain"
    )

    assert all(not b.captions for b in timeline_from_json(out.json_["timeline"]).beats)


async def test_crossfades_are_shortened_to_what_each_clip_can_pay_for() -> None:
    """Beat 1 plays 5.9s of a 6.0s clip, so only 3 frames of tail exist to
    dissolve from — the requested 6 is cut to 3 rather than failing
    validation."""
    out = await _assemble(
        _narration([0.0, 5.9, 10.0], duration=14.0), {1: 6.0, 2: 6.0, 3: 6.0}, crossfade_frames=6
    )
    beats = timeline_from_json(out.json_["timeline"]).beats

    assert beats[0].transition.kind == "none"
    assert (beats[1].transition.kind, beats[1].transition.frames) == ("crossfade", 3)
    assert (beats[2].transition.kind, beats[2].transition.frames) == ("crossfade", 6)


async def test_zero_crossfade_means_hard_cuts() -> None:
    out = await _assemble(
        _narration([0.0, 4.0], duration=8.0), {1: 6.0, 2: 6.0}, crossfade_frames=0
    )

    assert all(b.transition.kind == "none" for b in timeline_from_json(out.json_["timeline"]).beats)


async def test_one_item_carries_the_timeline_and_every_artifact_it_names() -> None:
    out = await _assemble(_narration([0.0, 4.0], duration=8.0), {1: 6.0, 2: 6.0})
    timeline = timeline_from_json(out.json_["timeline"])
    binary = out.binary or {}

    assert set(binary) == {"narration", clip_attachment(1), clip_attachment(2)}
    assert binary["narration"].id == timeline.narration.ref_id == NARRATION_REF.id
    assert [binary[clip_attachment(n)].id for n in (1, 2)] == [
        b.clip.ref_id for b in timeline.beats
    ]


async def test_clips_are_placed_by_beat_number_not_by_arrival_order() -> None:
    narration = _narration([0.0, 4.0, 8.0], duration=12.0)
    ctx = FakeExecutionContext(inputs={NARRATION_PORT: [narration]})
    clips = await _clips(ctx, {1: 6.0, 2: 6.0, 3: 6.0})
    ctx._inputs["main"] = list(reversed(clips))
    (out,) = (await AssembleNode().execute(ctx))["main"]

    beats = timeline_from_json(out.json_["timeline"]).beats
    assert [b.clip.ref_id for b in beats] == [c.json_["asset_id"] for c in clips]


async def test_what_assemble_emits_is_what_compose_accepts() -> None:
    """The seam between the two nodes, checked without rendering: compose's
    own ref resolution finds every artifact on the assembled item."""
    out = await _assemble(_narration([0.0, 4.0], duration=8.0), {1: 6.0, 2: 6.0})
    ctx = FakeExecutionContext(inputs={"main": [out]})
    timeline = timeline_from_json(out.json_["timeline"])
    wanted = {timeline.narration.ref_id, *(b.clip.ref_id for b in timeline.beats)}

    found, _ = ComposeNode()._resolve_refs(ctx, wanted)
    assert set(found) == wanted


# --- refusals ------------------------------------------------------------------


async def test_a_failed_beat_is_refused_by_number_not_rendered_around() -> None:
    with pytest.raises(NodeConfigurationError, match=r"beats 2, 4 of 5 have no clip.*Re-run only"):
        await _assemble(
            _narration([0.0, 4.0, 8.0, 12.0, 16.0], duration=20.0),
            {n: 6.0 for n in range(1, 6)},
            skip=(2, 4),
        )


async def test_a_beat_arriving_twice_is_refused() -> None:
    narration = _narration([0.0, 4.0], duration=8.0)
    dup = Item.model_validate({"json": {"beat_number": 1, "asset_id": "x", "duration_seconds": 6}})
    with pytest.raises(NodeConfigurationError, match="beat 1 arrived more than once"):
        await _assemble(narration, {1: 6.0, 2: 6.0}, extra_clips=[dup])


async def test_a_clip_for_a_beat_the_narration_never_had_is_refused() -> None:
    narration = _narration([0.0, 4.0], duration=8.0)
    stray = Item.model_validate(
        {"json": {"beat_number": 7, "asset_id": "x", "duration_seconds": 6}}
    )
    with pytest.raises(NodeConfigurationError, match="beat 7 arrived but the narration"):
        await _assemble(narration, {1: 6.0, 2: 6.0}, extra_clips=[stray])


async def test_a_clip_too_short_for_its_line_is_refused_with_advice() -> None:
    with pytest.raises(NodeConfigurationError, match="beat 2's narration runs 7.00s.*only 6.00s"):
        await _assemble(_narration([0.0, 4.0], duration=11.0), {1: 6.0, 2: 6.0})


async def test_a_beat_without_its_mark_is_named() -> None:
    narration = _narration([0.0, 4.0], duration=8.0)
    narration.json_["captions"] = narration.json_["captions"][:1]
    with pytest.raises(NodeConfigurationError, match="no timing mark for beat 2.*'beat-2'"):
        await _assemble(narration, {1: 6.0, 2: 6.0})


async def test_a_clip_missing_from_the_library_is_named() -> None:
    narration = _narration([0.0], duration=4.0)
    gone = Item.model_validate(
        {"json": {"beat_number": 1, "asset_id": "ast_gone", "duration_seconds": 6}}
    )
    ctx = FakeExecutionContext(inputs={NARRATION_PORT: [narration], "main": [gone]})
    with pytest.raises(NodeConfigurationError, match="asset ast_gone.*not in this workspace"):
        await AssembleNode().execute(ctx)


async def test_no_narration_input_says_where_to_connect_it() -> None:
    ctx = FakeExecutionContext(inputs={"main": []})
    with pytest.raises(NodeConfigurationError, match="exactly one narration item.*got 0"):
        await AssembleNode().execute(ctx)


async def test_marks_that_collide_on_one_frame_are_refused_not_guessed() -> None:
    with pytest.raises(NodeConfigurationError, match="round to the same frame"):
        await _assemble(_narration([0.0, 4.0, 4.01], duration=9.0), {1: 6.0, 2: 6.0, 3: 6.0})
