"""`shortvideo.minimax_submit` — the matrix it checks and the retries it refuses.

Two groups carry the weight. The first is the model matrix: every limit is
checked locally, so a wrong duration or resolution fails before it reaches a
paid API. The second is the retry split, which is stricter here than anywhere
else in the plugin because MiniMax documents no idempotency key — an
automatically retried create is a second charge that nothing can recognise as
a duplicate (§5.3).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import NodeTestKit

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE
from tamtree_shortvideo.minimax import (
    MAX_PROMPT_CHARACTERS,
    MODELS,
    MinimaxError,
    MinimaxSubmitAmbiguous,
    MinimaxUnavailable,
)
from tamtree_shortvideo.minimax_submit import MinimaxSubmitNode

TOKEN = "eyJ-a-real-looking-minimax-key"
TASK_ID = "video_task_01H9Z"
REQUEST_ID = "trace-abc-123"
PROMPT = "A coin stack growing, shallow depth of field, warm light."

CREDENTIAL = {"token": TOKEN, "test_url": "https://api.minimax.io/v1/models"}

DEFAULTS: dict[str, Any] = {
    "model": "MiniMax-H3",
    "prompt": PROMPT,
    "duration_seconds": 6,
    "resolution": "768P",
    "ratio": "9:16",
    "prompt_expansion_mode": "balanced",
}


def _accepted() -> httpx.Response:
    return httpx.Response(200, json={"task_id": TASK_ID, "request_id": REQUEST_ID})


def _refused(
    status: int, *, error_type: str = "invalid_params", message: str = "bad"
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "error",
            "error": {"type": error_type, "message": message, "http_code": status},
            "request_id": REQUEST_ID,
        },
    )


def _kit(
    *,
    responses: list[httpx.Response] | None = None,
    inputs: list[Item] | None = None,
    credential: dict[str, str] | None = None,
    **overrides: Any,
) -> NodeTestKit:
    kit = (
        NodeTestKit(MinimaxSubmitNode())
        .params(**{**DEFAULTS, **overrides})
        .credentials({MINIMAX_CREDENTIAL_TYPE: credential or CREDENTIAL})
        .responses(responses or [_accepted()])
    )
    if inputs is not None:
        kit.inputs("main", inputs)
    return kit


def _request_body(kit: NodeTestKit, index: int = 0) -> dict[str, Any]:
    return json.loads(kit.requests[index].content.decode("utf-8"))


# -- the create request ------------------------------------------------------


async def test_a_beat_becomes_a_persisted_task_id() -> None:
    """The node's entire job: a prompt in, an id Tamtree has written down out.
    It does not wait for the clip — that is D3's whole point."""
    kit = _kit()

    (item,) = (await kit.run())["main"]

    assert item.json_["task_id"] == TASK_ID
    assert item.json_["request_id"] == REQUEST_ID
    assert item.json_["model"] == "MiniMax-H3"
    assert item.json_["duration_seconds"] == 6
    assert item.json_["resolution"] == "768P"
    assert item.json_["ratio"] == "9:16"
    assert item.json_["prompt"] == PROMPT
    assert item.json_["submitted_at"] > 0


async def test_it_posts_the_v2_multimodal_content_shape() -> None:
    """The create contract takes a `content` array with a text element, not a
    flat `prompt` field. Getting this wrong is a 400 that costs a run."""
    kit = _kit()

    await kit.run()

    assert str(kit.requests[0].url) == "https://api.minimax.io/v2/video_generation"
    assert kit.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert _request_body(kit) == {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": PROMPT}],
        "resolution": "768P",
        "duration": 6,
        "ratio": "9:16",
        "extra": {"prompt_expansion_mode": "balanced"},
    }


async def test_the_duration_is_sent_as_an_integer() -> None:
    """MiniMax's `duration` is an integer. A float here is a 400."""
    kit = _kit(duration_seconds=8.0)

    await kit.run()

    duration = _request_body(kit)["duration"]
    assert duration == 8
    assert isinstance(duration, int)


async def test_no_callback_url_is_ever_sent() -> None:
    """MiniMax callbacks need a challenge-response endpoint Tamtree does not
    have (§7). Absent by construction, not by default."""
    kit = _kit()

    await kit.run()

    assert "callback_url" not in _request_body(kit)


async def test_the_source_json_and_attachments_survive() -> None:
    """A beat arrives with its narration and audio from Wave 1 and must still
    carry both when it reaches collection (§9)."""
    beat = Item.model_validate(
        {
            "json": {"narration": "Compound interest.", "duration_seconds": 4.2},
            "binary": {
                "audio": {
                    "id": "b1",
                    "mime_type": "audio/wav",
                    "size_bytes": 3,
                    "storage_key": "ws/ws_test/binary/b1",
                }
            },
        }
    )
    kit = _kit(inputs=[beat])

    (item,) = (await kit.run())["main"]

    assert item.json_["narration"] == "Compound interest."
    assert item.binary is not None
    assert "audio" in item.binary
    # The node's own duration wins the collision — it is this step's answer.
    assert item.json_["duration_seconds"] == 6


async def test_one_submission_per_input_item() -> None:
    kit = _kit(
        responses=[_accepted(), _accepted()],
        inputs=[
            Item.model_validate({"json": {"beat": 1}}),
            Item.model_validate({"json": {"beat": 2}}),
        ],
    )

    output = await kit.run()

    assert [item.json_["beat"] for item in output["main"]] == [1, 2]
    assert len(kit.requests) == 2


# -- the model matrix, checked before anything is billed ---------------------


@pytest.mark.parametrize(
    ("model", "duration"),
    [("MiniMax-H3", 3), ("MiniMax-H3", 16), ("MiniMax-H3-Max", 4), ("MiniMax-H3-Max", 16)],
)
async def test_a_duration_outside_the_model_range_is_refused_locally(
    model: str, duration: int
) -> None:
    kit = _kit(model=model, duration_seconds=duration, resolution="768P")

    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    limits = MODELS[model]
    assert f"{limits.min_seconds}–{limits.max_seconds} seconds" in str(caught.value)
    assert kit.requests == []  # nothing was submitted


@pytest.mark.parametrize(
    ("model", "duration"),
    [("MiniMax-H3", 4), ("MiniMax-H3", 15), ("MiniMax-H3-Max", 5), ("MiniMax-H3-Max", 15)],
)
async def test_the_ends_of_each_range_are_accepted(model: str, duration: int) -> None:
    kit = _kit(model=model, duration_seconds=duration, resolution="768P")

    await kit.run()

    assert _request_body(kit)["duration"] == duration


async def test_a_resolution_the_model_does_not_offer_names_the_one_that_does() -> None:
    """2K is MiniMax-H3 only. Saying so beats echoing `invalid_params`."""
    kit = _kit(model="MiniMax-H3-Max", resolution="2K")

    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    message = str(caught.value)
    assert "MiniMax-H3-Max does not generate at 2K" in message
    assert "MiniMax-H3 does" in message
    assert kit.requests == []


async def test_480p_is_refused_on_the_model_that_lacks_it() -> None:
    kit = _kit(model="MiniMax-H3", resolution="480P")

    with pytest.raises(NodeConfigurationError, match="does not generate at 480P"):
        await kit.run()


async def test_an_unknown_model_says_the_matrix_needs_updating() -> None:
    """A model this plugin does not know has limits it cannot check, and
    guessing them would put a billed rejection back on the table."""
    kit = _kit(model="MiniMax-H4")

    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    assert "model matrix in this plugin needs updating" in str(caught.value)
    assert kit.requests == []


async def test_a_fractional_duration_is_refused_rather_than_rounded() -> None:
    """Rounding silently would change what the beat costs without saying so."""
    kit = _kit(duration_seconds=6.5)

    with pytest.raises(NodeConfigurationError, match="whole seconds only"):
        await kit.run()


async def test_a_missing_duration_is_refused_rather_than_defaulted() -> None:
    kit = _kit(duration_seconds=None)

    with pytest.raises(NodeConfigurationError, match="no sensible default to spend"):
        await kit.run()


async def test_an_empty_prompt_is_refused() -> None:
    kit = _kit(prompt="   ")

    with pytest.raises(NodeConfigurationError, match="visual prompt is empty"):
        await kit.run()


async def test_an_over_long_prompt_is_refused_locally() -> None:
    kit = _kit(prompt="a" * (MAX_PROMPT_CHARACTERS + 1))

    with pytest.raises(NodeConfigurationError, match="describing more than one shot"):
        await kit.run()

    assert kit.requests == []


async def test_an_unsupported_ratio_is_refused() -> None:
    kit = _kit(ratio="7:3")

    with pytest.raises(NodeConfigurationError, match="does not accept the aspect ratio"):
        await kit.run()


async def test_a_missing_api_key_is_refused_before_the_call() -> None:
    kit = _kit(credential={"token": "  "})

    with pytest.raises(NodeConfigurationError, match="has no API key"):
        await kit.run()

    assert kit.requests == []


# -- the retry split: §5.3's binding -----------------------------------------


async def test_a_rate_limit_keeps_its_retry_budget() -> None:
    """429 means rejected before processing. Nothing was created, so a retry
    costs nothing — this is the one status that stays retryable."""
    kit = _kit(responses=[httpx.Response(429, json={"request_id": REQUEST_ID})])

    with pytest.raises(MinimaxUnavailable) as caught:
        await kit.run()

    assert not isinstance(caught.value, NodeConfigurationError)
    assert REQUEST_ID in str(caught.value)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_on_create_is_not_retried_automatically(status: int) -> None:
    """The crux of §5.3. A 5xx does not say whether MiniMax accepted the clip,
    and an automatic retry would charge twice with no idempotency key able to
    recognise the first. So: named, non-retryable, and a person decides."""
    kit = _kit(responses=[httpx.Response(status, json={"request_id": REQUEST_ID})])

    with pytest.raises(MinimaxSubmitAmbiguous) as caught:
        await kit.run()

    message = str(caught.value)
    assert isinstance(caught.value, NodeConfigurationError)  # non-retryable
    assert "unknown whether the clip was accepted" in message
    assert "not retried automatically" in message
    assert REQUEST_ID in message


async def test_a_dropped_connection_on_create_is_not_retried_automatically() -> None:
    """Same reasoning as the 5xx, and the more likely case: the request may
    have arrived and the answer been lost."""
    kit = _kit()
    ctx = kit.context()
    ctx.http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("reset"))
        )
    )

    with pytest.raises(MinimaxSubmitAmbiguous) as caught:
        await MinimaxSubmitNode().execute(ctx)

    assert isinstance(caught.value, NodeConfigurationError)
    assert "may already be billing" in str(caught.value)


async def test_a_clean_refusal_is_named_and_not_retried() -> None:
    """A 400 was understood and rejected. Nothing was created, and a second
    identical attempt would be rejected identically."""
    kit = _kit(responses=[_refused(400, message="duration out of range")])

    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    message = str(caught.value)
    assert "invalid_params" in message
    assert "duration out of range" in message
    assert REQUEST_ID in message


async def test_an_accepted_request_with_no_task_id_is_the_worst_case_and_is_named() -> None:
    """A clip may be generating and billing with nothing to collect it by.
    Not retried, for the same reason a 5xx is not."""
    kit = _kit(responses=[httpx.Response(200, json={"request_id": REQUEST_ID})])

    with pytest.raises(MinimaxSubmitAmbiguous) as caught:
        await kit.run()

    message = str(caught.value)
    assert "returned no task id" in message
    assert REQUEST_ID in message


async def test_every_error_after_a_response_carries_the_provider_request_id() -> None:
    """§5.3's binding, stated as a test: the request id is the only handle a
    duplicate charge can be traced back to the submission that caused it."""
    for response in (
        httpx.Response(429, json={"request_id": REQUEST_ID}),
        httpx.Response(500, json={"request_id": REQUEST_ID}),
        _refused(400),
        httpx.Response(200, json={"request_id": REQUEST_ID}),
    ):
        kit = _kit(responses=[response])
        with pytest.raises((MinimaxError, MinimaxSubmitAmbiguous, MinimaxUnavailable)) as caught:
            await kit.run()
        assert REQUEST_ID in str(caught.value), response.status_code


async def test_a_trace_id_in_a_header_is_found_too() -> None:
    """Some gateways set the id as a header rather than in the body."""
    kit = _kit(
        responses=[httpx.Response(500, headers={"x-request-id": "hdr-9"}, json={})],
    )

    with pytest.raises(MinimaxSubmitAmbiguous, match="hdr-9"):
        await kit.run()


async def test_no_error_ever_quotes_the_api_key() -> None:
    kit = _kit(responses=[_refused(401, error_type="unauthorized", message="bad key")])

    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert TOKEN not in str(caught.value)


async def test_the_api_key_is_not_repeated_into_the_output_item() -> None:
    (item,) = (await _kit().run())["main"]

    assert TOKEN not in json.dumps(item.json_)


# -- cost: reported at collection, not here ----------------------------------


async def test_submit_reports_no_usage() -> None:
    """MiniMax prices a clip by the seconds it actually produced, and that
    figure arrives with the finished task — so `minimax_collect` reports the
    provider's own number rather than this step estimating from what was asked
    for. Reporting here with no cost would make the row count against
    `unpriced_block_count` for nothing."""
    kit = _kit()
    ctx = kit.context()

    await MinimaxSubmitNode().execute(ctx)

    assert ctx.usage == []
