"""V4.4: a replay buys nothing twice.

`shortvideo.reuse` is what makes "re-run with the same shot list" a recovery
rather than a second bill. The properties that matter are the ones a wrong
implementation would get silently wrong: the same inputs must find the same
artifact across processes, and *different* inputs — one changed prompt, one
changed resolution — must not.
"""

from __future__ import annotations

from typing import Any

import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.reuse import FIELDS_KEY, ReuseNode, reuse_name

KEY = {"model": "MiniMax-H3", "prompt": "a calm sea", "duration_seconds": 6, "resolution": "768P"}
BEAT = {"beat_number": 3, "narration_text": "The sea.", "visual_prompt": "a calm sea"}


def test_the_same_inputs_name_the_same_artifact_whatever_the_order() -> None:
    reordered = dict(reversed(list(KEY.items())))

    assert reuse_name("clip", KEY) == reuse_name("clip", reordered)
    assert reuse_name("clip", KEY).startswith("clip-")


@pytest.mark.parametrize(
    "change",
    [{"prompt": "a stormy sea"}, {"resolution": "1080P"}, {"duration_seconds": 10}],
)
def test_any_input_that_changes_the_artifact_changes_the_name(change: dict[str, Any]) -> None:
    assert reuse_name("clip", KEY) != reuse_name("clip", {**KEY, **change})


def test_the_kind_is_part_of_the_name() -> None:
    assert reuse_name("clip", KEY) != reuse_name("narration", KEY)


async def _run(ctx: FakeExecutionContext) -> dict[str, list[Item]]:
    return await ReuseNode().execute(ctx)


def _ctx(**params: Any) -> FakeExecutionContext:
    return FakeExecutionContext(
        inputs={"main": [Item.model_validate({"json": BEAT})]},
        params={"prefix": "clip", "key": KEY, "attachment": "video", **params},
    )


async def test_nothing_saved_goes_to_missing_with_the_name_to_save_under() -> None:
    result = await _run(_ctx())

    assert result["found"] == []
    (item,) = result["missing"]
    assert item.json_ == {**BEAT, "asset_name": reuse_name("clip", KEY), "reused": False}


async def test_a_saved_artifact_is_found_attached_and_carries_its_saved_fields() -> None:
    ctx = _ctx()
    info = await ctx.assets.save(
        data=b"clip bytes",
        name=reuse_name("clip", KEY),
        mime_type="video/mp4",
        metadata={"tags": ["shortvideo"], FIELDS_KEY: {"duration_seconds": 6.0, "task_id": "t-1"}},
    )
    result = await _run(ctx)

    assert result["missing"] == []
    (item,) = result["found"]
    assert item.json_["asset_id"] == info.id
    assert item.json_["reused"] is True
    assert item.json_["duration_seconds"] == 6.0
    assert item.json_["task_id"] == "t-1"
    # The beat's own identity is the item's, not the run that first made it.
    assert item.json_["beat_number"] == 3
    ref = (item.binary or {})["video"]
    assert ref.id == info.id and ref.mime_type == "video/mp4"


async def test_a_clip_saved_for_a_different_prompt_is_not_reused() -> None:
    ctx = _ctx()
    await ctx.assets.save(
        data=b"other clip",
        name=reuse_name("clip", {**KEY, "prompt": "a stormy sea"}),
        mime_type="video/mp4",
    )
    result = await _run(ctx)

    assert result["found"] == [] and len(result["missing"]) == 1


async def test_each_item_is_routed_on_its_own() -> None:
    """One loop pass is one beat, but the node must not assume it."""
    ctx = FakeExecutionContext(
        inputs={
            "main": [
                Item.model_validate({"json": {"p": "a calm sea"}}),
                Item.model_validate({"json": {"p": "a stormy sea"}}),
            ]
        },
        params={"prefix": "clip", "key": "{{ unused }}"},
    )
    await ctx.assets.save(
        data=b"calm",
        name=reuse_name("clip", {"p": "a calm sea"}),
        mime_type="video/mp4",
    )

    # FakeExecutionContext does not resolve expressions, so drive the key per
    # item the way the engine would, by overriding `param`.
    def param(name: str, item: Item | None = None) -> Any:
        if name == "key":
            return {"p": (item.json_ if item else {})["p"]}
        return {"prefix": "clip"}.get(name)

    ctx.param = param  # type: ignore[method-assign]
    result = await _run(ctx)

    assert [i.json_["p"] for i in result["found"]] == ["a calm sea"]
    assert [i.json_["p"] for i in result["missing"]] == ["a stormy sea"]


async def test_an_empty_key_is_refused_rather_than_matching_everything() -> None:
    with pytest.raises(NodeConfigurationError, match="no key"):
        await _run(_ctx(key={}))


async def test_the_kind_must_be_a_plain_word() -> None:
    with pytest.raises(NodeConfigurationError, match="short lowercase word"):
        await _run(_ctx(prefix="../clip"))
