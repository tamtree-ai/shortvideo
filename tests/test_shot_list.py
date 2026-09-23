"""V4.1: a script is held to the pipeline's shape before anything is paid for.

Every refusal here is a refusal that would otherwise have happened after the
narration call, or after the first MiniMax generation — or not at all, as a
short whose captions overflow. So each test asserts the step fails *by name*
(which beat, which limit), since a refusal the author cannot act on is only
slightly better than a bad render.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.shot_list import (
    HARD_MAX_BEATS,
    ShotListNode,
    beat_mark,
    parse_script,
)
from tamtree_shortvideo.timeline import MAX_CAPTION_CHARACTERS


def _beats(count: int, **overrides: Any) -> list[dict[str, Any]]:
    return [
        {"narration": f"Line number {n}.", "visual_prompt": f"Shot {n}, slow push in.", **overrides}
        for n in range(1, count + 1)
    ]


async def _run(text: Any = None, **params: Any) -> Item:
    item = Item.model_validate({"json": {"text": text}}) if text is not None else Item()
    ctx = FakeExecutionContext(inputs={"main": [item]}, params=params)
    (out,) = (await ShotListNode().execute(ctx))["main"]
    return out


async def test_a_good_script_is_numbered_from_one() -> None:
    out = await _run(json.dumps({"beats": _beats(3)}))

    assert [b["beat_number"] for b in out.json_["beats"]] == [1, 2, 3]
    assert out.json_["beats"][1] == {
        "beat_number": 2,
        "narration_text": "Line number 2.",
        "visual_prompt": "Shot 2, slow push in.",
    }
    assert out.json_["beat_count"] == 3


async def test_each_beat_s_phrase_carries_the_mark_assemble_reads() -> None:
    out = await _run(json.dumps({"beats": _beats(2)}))

    assert out.json_["phrases"] == [
        {"text": "Line number 1.", "name": beat_mark(1)},
        {"text": "Line number 2.", "name": beat_mark(2)},
    ]


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"beats": [{"narration": "A.", "visual_prompt": "B."}]}\n```',
        'Here is your script:\n{"beats": [{"narration": "A.", "visual_prompt": "B."}]}\nEnjoy!',
        '[{"narration": "A.", "visual_prompt": "B."}]',
    ],
)
async def test_the_shapes_a_model_actually_replies_in_are_read(reply: str) -> None:
    out = await _run(reply)

    assert out.json_["beats"][0]["narration_text"] == "A."


async def test_the_script_param_wins_over_the_incoming_text() -> None:
    ctx = FakeExecutionContext(
        inputs={"main": [Item.model_validate({"json": {"text": "not json at all"}})]},
        params={"script": {"beats": _beats(1)}},
    )
    (out,) = (await ShotListNode().execute(ctx))["main"]

    assert out.json_["beat_count"] == 1


async def test_prose_is_refused_and_quoted() -> None:
    with pytest.raises(NodeConfigurationError, match="not JSON.*It began: 'Once upon"):
        await _run("Once upon a time there was a short video.")


async def test_no_script_at_all_says_where_it_looked() -> None:
    with pytest.raises(NodeConfigurationError, match="Script parameter is blank"):
        await _run()


def test_an_empty_list_is_refused() -> None:
    with pytest.raises(NodeConfigurationError, match="no beats"):
        parse_script({"beats": []}, max_beats=8, max_narration_characters=90)


def test_an_object_without_beats_is_refused() -> None:
    with pytest.raises(NodeConfigurationError, match="no beat list"):
        parse_script({"scenes": _beats(2)}, max_beats=8, max_narration_characters=90)


def test_more_beats_than_allowed_is_refused_before_any_are_bought() -> None:
    with pytest.raises(NodeConfigurationError, match="9 beats.*at most 8.*paid video generation"):
        parse_script(_beats(9), max_beats=8, max_narration_characters=90)


async def test_the_beat_ceiling_cannot_be_raised_past_the_loop_s_cap() -> None:
    with pytest.raises(NodeConfigurationError, match=f"between 1 and {HARD_MAX_BEATS}"):
        await _run(json.dumps(_beats(2)), max_beats=HARD_MAX_BEATS + 1)


def test_a_beat_with_no_visual_prompt_is_named() -> None:
    beats = _beats(3)
    beats[1]["visual_prompt"] = "  "
    with pytest.raises(NodeConfigurationError, match="Beat 2 has no visual prompt"):
        parse_script(beats, max_beats=8, max_narration_characters=90)


def test_a_beat_with_no_narration_is_named() -> None:
    beats = _beats(3)
    del beats[2]["narration"]
    with pytest.raises(NodeConfigurationError, match="Beat 3 has no narration"):
        parse_script(beats, max_beats=8, max_narration_characters=90)


def test_a_beat_that_is_not_an_object_is_named() -> None:
    with pytest.raises(NodeConfigurationError, match="Beat 2 is str"):
        parse_script([_beats(1)[0], "just words"], max_beats=8, max_narration_characters=90)


def test_a_line_longer_than_one_caption_is_refused_with_advice() -> None:
    beats = _beats(1, narration="x" * (MAX_CAPTION_CHARACTERS + 1))
    with pytest.raises(NodeConfigurationError, match="Beat 1's narration is 91.*split the line"):
        parse_script(beats, max_beats=8, max_narration_characters=MAX_CAPTION_CHARACTERS)


async def test_the_line_limit_cannot_be_raised_past_a_caption() -> None:
    with pytest.raises(NodeConfigurationError, match=f"between 10 and {MAX_CAPTION_CHARACTERS}"):
        await _run(json.dumps(_beats(1)), max_narration_characters=MAX_CAPTION_CHARACTERS + 1)


def test_the_alternative_field_names_are_accepted() -> None:
    (beat,) = parse_script(
        [{"narration_text": "A.", "visual": "B."}], max_beats=8, max_narration_characters=90
    )

    assert beat == {"beat_number": 1, "narration_text": "A.", "visual_prompt": "B."}


def test_a_line_broken_across_lines_is_joined() -> None:
    (beat,) = parse_script(
        [{"narration": "Two\n  halves.", "visual_prompt": "B."}],
        max_beats=8,
        max_narration_characters=90,
    )

    assert beat["narration_text"] == "Two halves."


async def test_a_narration_too_big_for_one_synthesis_call_is_refused_here() -> None:
    """Twenty-five 90-character lines of multi-byte text outgrow Google's
    5,000-byte request — refused before the narration step spends anything."""
    beats = [{"narration": "日" * 90, "visual_prompt": "B."} for _ in range(HARD_MAX_BEATS)]
    with pytest.raises(NodeConfigurationError, match="synchronous synthesize limit"):
        await _run(json.dumps(beats), max_beats=HARD_MAX_BEATS)
