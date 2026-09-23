"""The OpenRouter video nodes — MiniMax's footage, paid in OpenRouter credits.

The same two properties carry the weight as on the direct MiniMax route, and
the tests are grouped by them:

- **The retry split.** A create documents no idempotency key, so every
  ambiguous create is a named, non-retryable refusal; a poll creates nothing,
  so it keeps its retry budget. Getting either the wrong way round is a real
  bug — a double charge, or a clip abandoned over a blip.
- **Cost is OpenRouter's number.** `usage.cost` on the completed job, else the
  generation ledger, else unpriced — never a rate, and never a guess.

Plus what is new on this route: 402/403 are named as the credit and
spend-limit refusals they are, `generate_audio` is always off, and the bearer
token only ever goes to `openrouter.ai`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import NodeTestKit

from tamtree_shortvideo.credentials import OPENROUTER_CREDENTIAL_TYPE
from tamtree_shortvideo.openrouter import OpenRouterError, OpenRouterUnavailable
from tamtree_shortvideo.openrouter_video import (
    CREATE_URL,
    MAX_PROMPT_CHARACTERS,
    MODELS,
    OpenRouterVideoNotReady,
    OpenRouterVideoSubmitAmbiguous,
    content_url,
    job_url,
)
from tamtree_shortvideo.openrouter_video_collect import OpenRouterVideoCollectNode
from tamtree_shortvideo.openrouter_video_submit import OpenRouterVideoSubmitNode

TOKEN = "sk-or-v1-a-real-looking-openrouter-key"
CREDENTIAL = {"token": TOKEN}
JOB_ID = "gen-vid-1789480874-Ab3dEf9h"
GENERATION_ID = "gen-1789480874-xyz"
PROMPT = "A coin stack growing, shallow depth of field, warm light."

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64

SUBMIT_DEFAULTS: dict[str, Any] = {
    "model": "minimax/hailuo-3-max",
    "prompt": PROMPT,
    "duration_seconds": 6,
    "resolution": "768p",
    "ratio": "9:16",
}


def _accepted(**extra: Any) -> httpx.Response:
    return httpx.Response(
        202,
        json={
            "id": JOB_ID,
            "polling_url": f"/api/v1/videos/{JOB_ID}",
            "status": "pending",
            **extra,
        },
    )


def _refused(status: int, message: str = "bad") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


def _submit_kit(
    *,
    responses: list[httpx.Response] | None = None,
    inputs: list[Item] | None = None,
    **params: Any,
) -> NodeTestKit:
    kit = (
        NodeTestKit(OpenRouterVideoSubmitNode())
        .params(**{**SUBMIT_DEFAULTS, **params})
        .credentials({OPENROUTER_CREDENTIAL_TYPE: CREDENTIAL})
        .responses(responses or [_accepted()])
    )
    if inputs is not None:
        kit.inputs("main", inputs)
    return kit


def _body(kit: NodeTestKit, index: int = 0) -> dict[str, Any]:
    return json.loads(kit.requests[index].content.decode("utf-8"))


# -- submit: the request -----------------------------------------------------


async def test_a_beat_becomes_a_persisted_job_id() -> None:
    kit = _submit_kit(responses=[_accepted(generation_id=GENERATION_ID)])

    (item,) = (await kit.run())["main"]

    assert item.json_["task_id"] == JOB_ID
    assert item.json_["generation_id"] == GENERATION_ID
    assert item.json_["model"] == "minimax/hailuo-3-max"
    assert item.json_["duration_seconds"] == 6
    assert item.json_["resolution"] == "768p"
    assert item.json_["ratio"] == "9:16"
    assert item.json_["submitted_at"] > 0


async def test_it_posts_openrouters_flat_request_shape() -> None:
    kit = _submit_kit()
    await kit.run()

    (request,) = kit.requests
    assert request.method == "POST"
    assert str(request.url) == CREATE_URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert _body(kit) == {
        "model": "minimax/hailuo-3-max",
        "prompt": PROMPT,
        "duration": 6,
        "resolution": "768p",
        "aspect_ratio": "9:16",
        "generate_audio": False,
    }


async def test_the_models_own_soundtrack_is_always_turned_off() -> None:
    """H3 generates audio by default; the short's audio is the narration."""
    kit = _submit_kit(model="minimax/hailuo-3", resolution="2K")
    await kit.run()

    assert MODELS["minimax/hailuo-3"].generates_audio is True
    assert _body(kit)["generate_audio"] is False


async def test_minimax_spelling_of_a_resolution_is_accepted() -> None:
    """`768P` is what an author moving a flow off `minimax_submit` types."""
    kit = _submit_kit(resolution="768P")
    await kit.run()
    assert _body(kit)["resolution"] == "768p"


async def test_the_source_json_and_attachments_survive() -> None:
    source = Item.model_validate({"json": {"beat_number": 3, "narration_text": "hi"}})
    kit = _submit_kit(inputs=[source])

    (item,) = (await kit.run())["main"]

    assert item.json_["beat_number"] == 3
    assert item.json_["narration_text"] == "hi"


async def test_one_submission_per_input_item() -> None:
    kit = _submit_kit(
        inputs=[Item.model_validate({"json": {"n": n}}) for n in range(3)],
        responses=[_accepted(), _accepted(), _accepted()],
    )
    out = await kit.run()
    assert len(out["main"]) == 3
    assert len(kit.requests) == 3


# -- submit: local refusals (free, before anything is routed) ----------------


@pytest.mark.parametrize(
    ("params", "phrase"),
    [
        ({"duration_seconds": 4}, "5–15 seconds"),
        ({"duration_seconds": 16}, "5–15 seconds"),
        ({"duration_seconds": 6.5}, "whole seconds"),
        ({"duration_seconds": ""}, "Duration is required"),
        ({"resolution": "2K"}, "minimax/hailuo-3 does"),
        ({"model": "minimax/hailuo-3", "resolution": "768p"}, "minimax/hailuo-3-max does"),
        ({"model": "google/veo-3.1"}, "Unknown OpenRouter video model"),
        ({"ratio": "9:21"}, "aspect ratio"),
        ({"prompt": "   "}, "prompt is empty"),
        ({"prompt": "x" * (MAX_PROMPT_CHARACTERS + 1)}, "at most"),
    ],
)
async def test_a_value_the_model_cannot_take_is_refused_locally(
    params: dict[str, Any], phrase: str
) -> None:
    kit = _submit_kit(**params)
    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()
    assert phrase in str(caught.value)
    assert kit.requests == []


@pytest.mark.parametrize("duration", [5, 15])
async def test_the_ends_of_the_range_are_accepted(duration: int) -> None:
    kit = _submit_kit(duration_seconds=duration)
    await kit.run()
    assert _body(kit)["duration"] == duration


async def test_a_missing_api_key_is_refused_before_the_call() -> None:
    kit = (
        NodeTestKit(OpenRouterVideoSubmitNode())
        .params(**SUBMIT_DEFAULTS)
        .credentials({OPENROUTER_CREDENTIAL_TYPE: {"token": "  "}})
        .responses([_accepted()])
    )
    with pytest.raises(NodeConfigurationError):
        await kit.run()
    assert kit.requests == []


# -- submit: the retry split -------------------------------------------------


async def test_a_rate_limit_keeps_its_retry_budget() -> None:
    with pytest.raises(OpenRouterUnavailable):
        await _submit_kit(responses=[_refused(429)]).run()


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_on_create_is_not_retried_automatically(status: int) -> None:
    with pytest.raises(OpenRouterVideoSubmitAmbiguous) as caught:
        await _submit_kit(responses=[_refused(status)]).run()
    # Non-retryable is the point: a second create is a second charge.
    assert isinstance(caught.value, NodeConfigurationError)
    assert "Activity" in str(caught.value)


async def test_a_dropped_connection_on_create_is_not_retried_automatically() -> None:
    kit = _submit_kit()
    ctx = kit.context()
    ctx.http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("reset"))
        )
    )
    with pytest.raises(OpenRouterVideoSubmitAmbiguous):
        await OpenRouterVideoSubmitNode().execute(ctx)


async def test_too_few_credits_is_named_and_says_nothing_was_charged() -> None:
    with pytest.raises(OpenRouterError) as caught:
        await _submit_kit(responses=[_refused(402, "Insufficient credits")]).run()
    assert not isinstance(caught.value, OpenRouterVideoSubmitAmbiguous)
    assert "openrouter.ai/credits" in str(caught.value)
    assert "Nothing was generated or charged" in str(caught.value)


async def test_a_spend_limit_is_named() -> None:
    with pytest.raises(OpenRouterError) as caught:
        await _submit_kit(responses=[_refused(403, "Key limit exceeded")]).run()
    assert "spend limit" in str(caught.value)


async def test_a_clean_refusal_quotes_openrouter_and_is_not_retried() -> None:
    with pytest.raises(OpenRouterError) as caught:
        await _submit_kit(responses=[_refused(400, "duration must be one of 5..15")]).run()
    assert "duration must be one of 5..15" in str(caught.value)
    assert not isinstance(caught.value, OpenRouterVideoSubmitAmbiguous)


async def test_an_accepted_request_with_no_job_id_is_the_worst_case_and_is_named() -> None:
    response = httpx.Response(202, json={"status": "pending"})
    with pytest.raises(OpenRouterVideoSubmitAmbiguous):
        await _submit_kit(responses=[response]).run()


async def test_submit_reports_no_usage() -> None:
    kit = _submit_kit()
    context = kit.context()
    await OpenRouterVideoSubmitNode().execute(context)
    assert context.usage == []


async def test_no_error_ever_quotes_the_api_key() -> None:
    for response in (_refused(400), _refused(402), _refused(403), _refused(500)):
        with pytest.raises(NodeConfigurationError) as caught:
            await _submit_kit(responses=[response]).run()
        assert TOKEN not in str(caught.value)


# -- collect -----------------------------------------------------------------


def _job(status: str = "completed", **extra: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": JOB_ID,
        "polling_url": f"/api/v1/videos/{JOB_ID}",
        "status": status,
    }
    if status == "completed":
        job.update(
            generation_id=GENERATION_ID,
            unsigned_urls=[content_url(JOB_ID)],
            usage={"cost": 0.48, "is_byok": False},
        )
    job.update(extra)
    return job


SUBMITTED = Item.model_validate(
    {
        "json": {
            "beat_number": 2,
            "task_id": JOB_ID,
            "model": "minimax/hailuo-3-max",
            "duration_seconds": 6,
        }
    }
)


def _collect_kit(*, responses: list[httpx.Response] | None = None, **params: Any) -> NodeTestKit:
    return (
        NodeTestKit(OpenRouterVideoCollectNode())
        .params(
            **{
                "task_id": JOB_ID,
                "max_wait_seconds": 60,
                "poll_interval_seconds": 0.001,
                "max_download_megabytes": 1,
                "output_binary_property": "video",
                **params,
            }
        )
        .credentials({OPENROUTER_CREDENTIAL_TYPE: CREDENTIAL})
        .inputs("main", [SUBMITTED])
        .responses(
            responses or [httpx.Response(200, json=_job()), httpx.Response(200, content=MP4)]
        )
    )


async def test_a_finished_job_is_saved_with_openrouters_cost() -> None:
    kit = _collect_kit()
    context = kit.context()
    out = await OpenRouterVideoCollectNode().execute(context)

    (item,) = out["main"]
    assert item.json_["task_id"] == JOB_ID
    assert item.json_["status"] == "completed"
    assert item.json_["beat_number"] == 2  # the beat's own fields travel on
    assert item.json_["video"]["mime_type"] == "video/mp4"
    assert item.json_["video"]["size_bytes"] == len(MP4)
    assert "video" in item.binary
    assert item.json_["priced"] is True
    assert item.json_["cost_usd"] == "0.48"
    assert item.json_["billed_seconds"] == 6.0
    (usage,) = context.usage
    assert usage["provider"] == "openrouter"
    assert usage["model"] == "minimax/hailuo-3-max"
    assert usage["cost_usd"] == Decimal("0.48")


async def test_the_bearer_token_only_ever_goes_to_openrouter() -> None:
    kit = _collect_kit()
    await kit.run()

    assert [str(request.url) for request in kit.requests] == [job_url(JOB_ID), content_url(JOB_ID)]
    for request in kit.requests:
        assert request.url.host == "openrouter.ai"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_it_polls_until_the_job_completes() -> None:
    kit = _collect_kit(
        responses=[
            httpx.Response(200, json=_job("pending")),
            httpx.Response(200, json=_job("in_progress")),
            httpx.Response(200, json=_job()),
            httpx.Response(200, content=MP4),
        ]
    )
    (item,) = (await kit.run())["main"]
    assert item.json_["polls"] == 3


@pytest.mark.parametrize(
    ("status", "phrase"),
    [("failed", "could not generate"), ("cancelled", "was cancelled"), ("expired", "expired")],
)
async def test_a_job_that_ends_without_a_clip_is_named(status: str, phrase: str) -> None:
    kit = _collect_kit(
        responses=[httpx.Response(200, json=_job(status, error="Content policy violation"))]
    )
    with pytest.raises(OpenRouterError) as caught:
        await kit.run()
    assert phrase in str(caught.value)
    assert "Content policy violation" in str(caught.value)


async def test_running_out_of_time_is_not_ready_and_not_a_failure() -> None:
    kit = _collect_kit(
        max_wait_seconds=0.001,
        responses=[httpx.Response(200, json=_job("in_progress"))] * 50,
    )
    with pytest.raises(OpenRouterVideoNotReady) as caught:
        await kit.run()
    assert "running this step again on the same job id collects it" in str(caught.value)


async def test_an_unreachable_openrouter_keeps_the_polls_retry_budget() -> None:
    """The opposite of submit, on purpose: a poll creates nothing."""
    with pytest.raises(OpenRouterUnavailable):
        await _collect_kit(responses=[_refused(503)]).run()


@pytest.mark.parametrize("status", [409, 502])
async def test_content_not_yet_servable_is_worth_another_attempt(status: int) -> None:
    kit = _collect_kit(responses=[httpx.Response(200, json=_job()), _refused(status)])
    with pytest.raises(OpenRouterUnavailable):
        await kit.run()


async def test_an_error_page_is_not_saved_as_a_video() -> None:
    kit = _collect_kit(
        responses=[
            httpx.Response(200, json=_job()),
            httpx.Response(
                200, content=b"<html>oops</html>", headers={"content-type": "video/mp4"}
            ),
        ]
    )
    with pytest.raises(OpenRouterError) as caught:
        await kit.run()
    assert "not a video file" in str(caught.value)


async def test_an_over_size_clip_is_refused_with_the_setting_named() -> None:
    big = MP4 + b"\x00" * (2 * 1024 * 1024)
    kit = _collect_kit(
        responses=[httpx.Response(200, json=_job()), httpx.Response(200, content=big)]
    )
    with pytest.raises(OpenRouterError) as caught:
        await kit.run()
    assert "Refuse a clip larger than" in str(caught.value)


async def test_a_missing_cost_falls_back_to_the_generation_ledger() -> None:
    kit = _collect_kit(
        responses=[
            httpx.Response(200, json=_job(usage={})),
            httpx.Response(200, content=MP4),
            httpx.Response(200, json={"data": {"total_cost": 0.3, "tokens_prompt": 0}}),
        ]
    )
    context = kit.context()
    (item,) = (await OpenRouterVideoCollectNode().execute(context))["main"]

    assert item.json_["cost_usd"] == "0.3"
    assert GENERATION_ID in str(kit.requests[2].url)


async def test_no_cost_anywhere_is_reported_unpriced_rather_than_guessed() -> None:
    kit = _collect_kit(
        responses=[
            httpx.Response(200, json=_job(usage={})),
            httpx.Response(200, content=MP4),
            httpx.Response(500),
        ]
    )
    context = kit.context()
    (item,) = (await OpenRouterVideoCollectNode().execute(context))["main"]

    assert item.json_["priced"] is False
    assert item.json_["cost_usd"] == ""
    (usage,) = context.usage
    assert usage["cost_usd"] is None


async def test_no_job_id_is_a_configuration_error() -> None:
    with pytest.raises(NodeConfigurationError) as caught:
        await _collect_kit(task_id="  ").run()
    assert "$json.task_id" in str(caught.value)
