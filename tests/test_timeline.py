"""`TimelineV1` — the freeze, with a test per rule that could be relaxed by accident.

The contract itself is `02-timeline-v1.md` in the planning subproject; this
file is what stops the document and the code drifting apart. The groups follow
its sections: frame math, trim and pad, captions, transitions, audio, ceilings,
the digest, and the render document.

The helpers below build a *valid* timeline by construction, so every test is
one deliberate deviation from a thing that works — which is what makes a
failure readable as "this rule", not "this fixture".
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tamtree_shortvideo.timeline import (
    CLIP_DUCK_DB,
    MAX_BEATS,
    MAX_CAPTION_CHARACTERS,
    MAX_PAD_SECONDS,
    MAX_TRANSITION_FRAMES,
    NARRATION_TARGET_LUFS,
    Beat,
    Caption,
    Clip,
    Music,
    Narration,
    Timeline,
    TimelineError,
    Transition,
    as_json,
    beat_boundaries,
    canonical_json,
    frames_at,
    render_document,
    timeline_digest,
    timeline_from_json,
    validate,
)

FPS = 30


def _clip(index: int, *, seconds: float, pad: int = 0) -> Clip:
    """A clip long enough for its beat, with a tail left over for a crossfade."""
    played = seconds - pad / FPS
    return Clip(
        ref_id=f"bin_01J8XK{index}CLIP",
        mime_type="video/mp4",
        source_duration_seconds=played + 1.0,
        in_seconds=0.0,
        out_seconds=played,
    )


def _beat(index: int, *, start: int, frames: int, captions: bool = True, pad: int = 0) -> Beat:
    text = f"Beat {index} says something short."
    return Beat(
        index=index,
        start_frame=start,
        frames=frames,
        pad_frames=pad,
        clip=_clip(index, seconds=frames / FPS, pad=pad),
        captions=(Caption(text=text, start_frame=start, end_frame=start + frames),)
        if captions
        else (),
    )


def _timeline(
    *,
    beat_frames: tuple[int, ...] = (150, 150, 150),
    template: str = "short-captioned",
    **overrides: object,
) -> Timeline:
    beats: list[Beat] = []
    start = 0
    for index, frames in enumerate(beat_frames):
        beats.append(
            _beat(index, start=start, frames=frames, captions=template == "short-captioned")
        )
        start += frames
    return Timeline(
        template=template,  # type: ignore[arg-type]
        narration=Narration(
            ref_id="bin_01J8XKNARR",
            mime_type="audio/wav",
            duration_seconds=start / FPS,
        ),
        beats=tuple(beats),
        **overrides,  # type: ignore[arg-type]
    )


def test_the_fixture_is_valid() -> None:
    """Every other test is one deviation from this, so this one comes first."""
    timeline = _timeline()

    validate(timeline)

    assert timeline.total_frames == 450
    assert timeline.total_seconds == pytest.approx(15.0)


# -- frame math (section 3) --------------------------------------------------


def test_frames_round_half_up() -> None:
    assert frames_at(0.0) == 0
    assert frames_at(1.0) == 30
    assert frames_at(1.0 / 60) == 1  # exactly half a frame rounds up
    assert frames_at(0.016) == 0


def test_a_negative_or_infinite_time_is_not_a_boundary() -> None:
    with pytest.raises(TimelineError):
        frames_at(-0.1)
    with pytest.raises(TimelineError):
        frames_at(float("inf"))


def test_boundaries_come_from_absolute_marks_and_do_not_drift() -> None:
    """Twenty marks a third of a second apart. Cumulative rounding would drift;
    deriving each edge from its own absolute time cannot."""
    marks = [i / 3 for i in range(20)]
    duration = 20 / 3

    boundaries = beat_boundaries(marks, duration_seconds=duration)

    assert boundaries[0][0] == 0
    assert sum(frames for _, frames in boundaries) == frames_at(duration)
    for (start, frames), (next_start, _) in zip(boundaries, boundaries[1:], strict=False):
        assert start + frames == next_start


def test_beat_zero_starts_at_frame_zero_whatever_its_mark_says() -> None:
    boundaries = beat_boundaries([0.4, 2.0], duration_seconds=4.0)

    assert boundaries[0] == (0, 60)
    assert boundaries[1] == (60, 60)


def test_marks_must_increase_strictly() -> None:
    with pytest.raises(TimelineError, match="increase strictly"):
        beat_boundaries([0.0, 2.0, 2.0], duration_seconds=5.0)


def test_a_mark_past_the_measured_end_is_a_defect_not_a_clamp() -> None:
    with pytest.raises(TimelineError, match="past the measured end"):
        beat_boundaries([0.0, 6.0], duration_seconds=5.0)


def test_two_marks_in_the_same_frame_are_refused() -> None:
    with pytest.raises(TimelineError, match="same frame"):
        beat_boundaries([0.0, 1.0, 1.005], duration_seconds=5.0)


def test_beats_must_tile_the_narration_exactly() -> None:
    timeline = _timeline()
    longer = replace(
        timeline,
        narration=replace(timeline.narration, duration_seconds=timeline.total_seconds + 1.0),
    )

    with pytest.raises(TimelineError, match="timed against the audio"):
        validate(longer)


def test_a_gap_between_beats_is_refused() -> None:
    timeline = _timeline()
    moved = list(timeline.beats)
    moved[1] = replace(moved[1], start_frame=moved[1].start_frame + 1)

    with pytest.raises(TimelineError, match="no gap and no overlap"):
        validate(replace(timeline, beats=tuple(moved)))


def test_beats_are_numbered_in_order() -> None:
    timeline = _timeline()
    shuffled = (timeline.beats[1], timeline.beats[0], timeline.beats[2])

    with pytest.raises(TimelineError, match="numbered in order"):
        validate(replace(timeline, beats=shuffled))


# -- trim and pad (section 4) ------------------------------------------------


def test_trim_and_frame_count_must_agree() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(beats[0], clip=replace(beats[0].clip, out_seconds=4.0))

    with pytest.raises(TimelineError, match="trim and frame count must agree"):
        validate(replace(timeline, beats=tuple(beats)))


def test_a_clip_cannot_be_trimmed_past_its_own_end() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(beats[0], clip=replace(beats[0].clip, source_duration_seconds=2.0))

    with pytest.raises(TimelineError, match="only 2.0s long"):
        validate(replace(timeline, beats=tuple(beats)))


def test_a_short_clip_may_hold_its_last_frame_within_the_pad_ceiling() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = _beat(0, start=0, frames=150, pad=10)

    validate(replace(timeline, beats=tuple(beats)))


def test_a_pad_past_half_a_second_is_a_validation_error_not_a_frozen_frame() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = _beat(0, start=0, frames=150, pad=int(MAX_PAD_SECONDS * FPS) + 1)

    with pytest.raises(TimelineError, match="too short for the narration"):
        validate(replace(timeline, beats=tuple(beats)))


def test_the_same_clip_twice_is_read_as_an_aggregation_bug() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[2] = replace(beats[2], clip=replace(beats[2].clip, ref_id=beats[0].clip.ref_id))

    with pytest.raises(TimelineError, match="aggregation bug"):
        validate(replace(timeline, beats=tuple(beats)))


def test_only_the_containers_minimax_collect_produces_are_accepted() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(beats[0], clip=replace(beats[0].clip, mime_type="video/quicktime"))

    with pytest.raises(TimelineError, match="composition accepts"):
        validate(replace(timeline, beats=tuple(beats)))


# -- captions (section 5) ----------------------------------------------------


def test_short_plain_forbids_captions_and_short_captioned_requires_them() -> None:
    plain = _timeline(template="short-plain")
    validate(plain)

    with pytest.raises(TimelineError, match="renders none"):
        validate(replace(plain, beats=_timeline().beats))
    with pytest.raises(TimelineError, match="requires one per beat"):
        validate(replace(_timeline(), beats=plain.beats))


def test_a_caption_may_not_run_past_its_own_beat() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(
        beats[0], captions=(Caption(text="Too long.", start_frame=0, end_frame=151),)
    )

    with pytest.raises(TimelineError, match="past the end of"):
        validate(replace(timeline, beats=tuple(beats)))


def test_captions_within_a_beat_may_not_overlap() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(
        beats[0],
        captions=(
            Caption(text="First.", start_frame=0, end_frame=100),
            Caption(text="Second.", start_frame=80, end_frame=150),
        ),
    )

    with pytest.raises(TimelineError, match="overlapping"):
        validate(replace(timeline, beats=tuple(beats)))


def test_a_caption_longer_than_two_lines_is_refused() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(
        beats[0],
        captions=(Caption(text="x" * (MAX_CAPTION_CHARACTERS + 1), start_frame=0, end_frame=150),),
    )

    with pytest.raises(TimelineError, match="not a paragraph"):
        validate(replace(timeline, beats=tuple(beats)))


@pytest.mark.parametrize("text", ["", "   ", "line\x07bell", "zero​width"])
def test_empty_and_control_bearing_caption_text_is_refused(text: str) -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(beats[0], captions=(Caption(text=text, start_frame=0, end_frame=150),))

    with pytest.raises(TimelineError):
        validate(replace(timeline, beats=tuple(beats)))


def test_a_newline_is_the_one_control_character_a_caption_may_carry() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(
        beats[0], captions=(Caption(text="Two\nlines.", start_frame=0, end_frame=150),)
    )

    validate(replace(timeline, beats=tuple(beats)))


# -- transitions (section 6) -------------------------------------------------


def _with_transition(timeline: Timeline, index: int, transition: Transition) -> Timeline:
    beats = list(timeline.beats)
    beats[index] = replace(beats[index], transition=transition)
    return replace(timeline, beats=tuple(beats))


def test_a_crossfade_the_outgoing_tail_can_pay_for_is_accepted() -> None:
    validate(_with_transition(_timeline(), 1, Transition(kind="crossfade", frames=8)))


def test_beat_zero_has_nothing_to_dissolve_from() -> None:
    with pytest.raises(TimelineError, match="nothing to dissolve from"):
        validate(_with_transition(_timeline(), 0, Transition(kind="crossfade", frames=8)))


def test_a_crossfade_longer_than_half_a_second_is_refused() -> None:
    with pytest.raises(TimelineError, match="v1 allows"):
        validate(
            _with_transition(
                _timeline(), 1, Transition(kind="crossfade", frames=MAX_TRANSITION_FRAMES + 1)
            )
        )


def test_a_crossfade_may_not_cover_a_quarter_of_a_short_neighbour() -> None:
    timeline = _timeline(beat_frames=(150, 36, 264))

    with pytest.raises(TimelineError, match="a smear, not a cut"):
        validate(_with_transition(timeline, 1, Transition(kind="crossfade", frames=12)))


def test_a_crossfade_the_outgoing_clip_has_no_footage_for_is_refused() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    # Exactly as long as the beat it fills: nothing left past the out point.
    beats[0] = replace(
        beats[0], clip=replace(beats[0].clip, source_duration_seconds=beats[0].frames / FPS)
    )
    timeline = replace(timeline, beats=tuple(beats))

    with pytest.raises(TimelineError, match="only 0.000s exists"):
        validate(_with_transition(timeline, 1, Transition(kind="crossfade", frames=8)))


def test_an_unknown_transition_is_refused_rather_than_degraded() -> None:
    with pytest.raises(TimelineError, match="v1 renders"):
        validate(_with_transition(_timeline(), 1, Transition(kind="wipe", frames=8)))  # type: ignore[arg-type]


def test_no_transition_means_no_frames_of_one() -> None:
    with pytest.raises(TimelineError, match="claims 8 frames"):
        validate(_with_transition(_timeline(), 1, Transition(kind="none", frames=8)))


# -- audio (section 7) -------------------------------------------------------


@pytest.mark.parametrize("policy", ["mute", "duck", "keep"])
def test_the_three_clip_audio_policies_are_accepted(policy: str) -> None:
    validate(_timeline(clip_audio=policy))


def test_an_unknown_clip_audio_policy_is_refused() -> None:
    with pytest.raises(TimelineError, match="clip audio policy"):
        validate(_timeline(clip_audio="louder"))


def test_a_music_bed_shorter_than_the_video_is_refused_because_v1_does_not_loop() -> None:
    short = Music(ref_id="bin_01J8XKMUSIC", mime_type="audio/mpeg", duration_seconds=5.0)
    timeline = _timeline(music=short)

    with pytest.raises(TimelineError, match="does not loop"):
        validate(timeline)


def test_a_music_bed_that_covers_the_video_is_accepted() -> None:
    covering = Music(ref_id="bin_01J8XKMUSIC", mime_type="audio/mpeg", duration_seconds=60.0)
    validate(_timeline(music=covering))


def test_narration_must_be_a_format_wave_one_actually_produces() -> None:
    timeline = _timeline()

    with pytest.raises(TimelineError, match="composition accepts"):
        validate(replace(timeline, narration=replace(timeline.narration, mime_type="audio/aac")))


# -- ceilings (section 8) ----------------------------------------------------


def test_a_timeline_past_three_minutes_is_refused() -> None:
    timeline = _timeline(beat_frames=(450,) * 13)  # 195s

    with pytest.raises(TimelineError, match="v1 ceiling"):
        validate(timeline)


def test_a_timeline_under_three_seconds_has_nothing_to_approve() -> None:
    with pytest.raises(TimelineError, match="nothing to approve"):
        validate(_timeline(beat_frames=(60,)))


def test_more_than_thirty_beats_is_refused() -> None:
    with pytest.raises(TimelineError, match="exceeds the v1 ceiling"):
        validate(_timeline(beat_frames=(45,) * (MAX_BEATS + 1)))


def test_a_beat_longer_than_minimax_can_generate_has_no_honest_source() -> None:
    with pytest.raises(TimelineError, match="no honest source"):
        validate(_timeline(beat_frames=(480, 150)))


def test_a_beat_under_a_second_is_a_flash() -> None:
    with pytest.raises(TimelineError, match="the minimum is"):
        validate(_timeline(beat_frames=(20, 150, 280)))


def test_only_the_frozen_frame_shape_renders() -> None:
    with pytest.raises(TimelineError, match="1080x1920 only"):
        validate(_timeline(width=1920, height=1080))
    with pytest.raises(TimelineError, match="30fps only"):
        validate(_timeline(fps=60))


def test_a_future_timeline_version_is_refused_by_this_build() -> None:
    with pytest.raises(TimelineError, match="not version 2"):
        validate(_timeline(version=2))


def test_an_unknown_template_is_refused() -> None:
    with pytest.raises(TimelineError, match="unknown template"):
        validate(_timeline(template="cinematic"))


# -- the digest (section 10) -------------------------------------------------


def test_the_digest_is_stable_across_equal_timelines() -> None:
    assert timeline_digest(_timeline()) == timeline_digest(_timeline())


def test_changing_one_caption_changes_the_digest() -> None:
    timeline = _timeline()
    beats = list(timeline.beats)
    beats[0] = replace(
        beats[0], captions=(Caption(text="Something else.", start_frame=0, end_frame=150),)
    )

    assert timeline_digest(replace(timeline, beats=tuple(beats))) != timeline_digest(timeline)


def test_changing_a_ref_changes_the_digest() -> None:
    timeline = _timeline()
    swapped = replace(timeline, narration=replace(timeline.narration, ref_id="bin_01J8XKOTHER"))

    assert timeline_digest(swapped) != timeline_digest(timeline)


def test_canonical_json_is_compact_sorted_and_reparses_to_the_same_timeline() -> None:
    text = canonical_json(_timeline())
    parsed = json.loads(text)

    assert ", " not in text and '": ' not in text
    assert list(parsed) == sorted(parsed)
    assert canonical_json(timeline_from_json(parsed)) == text


def test_a_float_that_differs_below_the_rounding_floor_hashes_the_same() -> None:
    timeline = _timeline()
    nudged = replace(
        timeline,
        narration=replace(
            timeline.narration, duration_seconds=timeline.narration.duration_seconds + 1e-9
        ),
    )

    assert timeline_digest(nudged) == timeline_digest(timeline)


def test_a_round_trip_through_json_preserves_every_field() -> None:
    timeline = _timeline(
        music=Music(ref_id="bin_01J8XKMUSIC", mime_type="audio/mpeg", duration_seconds=60.0),
        clip_audio="duck",
    )
    timeline = _with_transition(timeline, 1, Transition(kind="crossfade", frames=8))

    restored = timeline_from_json(as_json(timeline))

    assert restored == timeline
    assert timeline_digest(restored) == timeline_digest(timeline)


def test_a_document_that_is_not_a_timeline_says_so() -> None:
    with pytest.raises(TimelineError, match="not a TimelineV1 document"):
        timeline_from_json({"template": "short-plain"})


# -- the render document (section 1) -----------------------------------------


def _paths(timeline: Timeline) -> dict[str, str]:
    paths = {timeline.narration.ref_id: "narration.wav"}
    for beat in timeline.beats:
        paths[beat.clip.ref_id] = f"clip-{beat.index:03d}.mp4"
    if timeline.music is not None:
        paths[timeline.music.ref_id] = "music.mp3"
    return paths


def test_the_render_document_carries_paths_and_never_a_ref() -> None:
    timeline = _timeline()

    document = render_document(timeline, _paths(timeline))

    assert document["narration"]["file"] == "narration.wav"
    assert "ref" not in document["narration"]
    assert [beat["clip"]["file"] for beat in document["beats"]] == [
        "clip-000.mp4",
        "clip-001.mp4",
        "clip-002.mp4",
    ]
    assert all("ref" not in beat["clip"] for beat in document["beats"])
    # No workspace-side identifier survives into the file the child reads. The fixture ref ids are
    # deliberately opaque, the way real BinaryRef ids are: an id that looked like `clip-0` would be
    # a substring of `clip-000.mp4` and this check would pass on the path alone.
    serialized = json.dumps(document)
    for ref_id in _paths(timeline):
        assert ref_id not in serialized


def test_the_render_document_carries_the_mix_constants_and_the_digest() -> None:
    timeline = _timeline()

    document = render_document(timeline, _paths(timeline))

    assert document["narration"]["target_lufs"] == NARRATION_TARGET_LUFS
    assert document["clip_duck_db"] == CLIP_DUCK_DB
    assert document["caption_safe_area"]["bottom"] == 0.84
    assert document["digest"] == timeline_digest(timeline)


def test_a_reference_with_no_materialized_file_is_refused() -> None:
    timeline = _timeline()
    paths = _paths(timeline)
    del paths["bin_01J8XK1CLIP"]

    with pytest.raises(TimelineError, match="no materialized file"):
        render_document(timeline, paths)
