"""`shortvideo.minimax_collect` — waiting honestly, and fetching before the URL dies.

Two things separate this node from every other one here, and nearly every test
is about one of them.

**It is the half of the split that is allowed to retry.** `minimax_submit`
refuses its retry budget because a second create is a second charge; a poll
creates nothing and costs nothing, so an unreachable provider here stays
retryable. The tests assert both directions, because getting them the same way
round would be a real bug in either node.

**Its output is a file, not a claim.** A finished task hands back a
time-limited URL, and everything that can go wrong between "succeeded" and
"saved" — an expired signature, an error page with a video `Content-Type`, a
clip bigger than the cap — has to fail by name rather than land in the
workspace as a `.mp4` that will not open.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import NodeTestKit

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE
from tamtree_shortvideo.minimax import MinimaxError, MinimaxNotReady, MinimaxUnavailable
from tamtree_shortvideo.minimax_collect import MinimaxCollectNode

TOKEN = "eyJ-a-real-looking-minimax-key"
TASK_ID = "video_task_01H9Z"
CLIP_URL = "https://cdn.minimax.io/video/01H9Z/output.mp4?sig=abc"

#: The smallest thing that sniffs as an MP4: a `ftyp` box at offset 4, which is
#: where the brand actually lives — the first four bytes are the box size.
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64


def task_body(
    status: str = "succeeded", *, url: str | None = CLIP_URL, **overrides: Any
) -> dict[str, Any]:
    """One `{"task": {...}}` answer, shaped as the query contract returns it."""
    task: dict[str, Any] = {
        "id": TASK_ID,
        "model": "MiniMax-H3",
        "status": status,
        "created_at": 1_758_240_000,
        "updated_at": 1_758_240_120,
        "resolution": "768P",
        "duration": 6,
        "ratio": "9:16",
        "task_type": "generation",
        "modality": "video",
    }
    if status == "succeeded":
        task["content"] = {"url": url} if url is not None else {}
        task["usage"] = {
            "total_seconds": 6,
            "input_seconds": 0,
            "output_seconds": 6,
            "input_image_count": 0,
        }
    task.update(overrides)
    return {"task": task}


def _kit(
    *,
    responses: list[httpx.Response] | None = None,
    inputs: list[Item] | None = None,
    **overrides: Any,
) -> NodeTestKit:
    kit = (
        NodeTestKit(MinimaxCollectNode())
        .params(
            **{
                "task_id": TASK_ID,
                "max_wait_seconds": 60,
                # Sub-millisecond, so a test that exercises the backoff still
                # runs in a test's worth of time.
                "poll_interval_seconds": 0.001,
                "max_download_megabytes": 1,
                "output_binary_property": "video",
                "price_usd_per_second": 0,
                **overrides,
            }
        )
        .credentials({MINIMAX_CREDENTIAL_TYPE: {"token": TOKEN}})
        .responses(
            responses or [httpx.Response(200, json=task_body()), httpx.Response(200, content=MP4)]
        )
    )
    if inputs is not None:
        kit.inputs("main", inputs)
    return kit


async def test_a_finished_task_is_saved_with_its_provenance() -> None:
    kit = _kit()
    out = await kit.run()

    (item,) = out["main"]
    assert item.json_["task_id"] == TASK_ID
    assert item.json_["status"] == "succeeded"
    assert item.json_["model"] == "MiniMax-H3"
    assert item.json_["resolution"] == "768P"
    assert item.json_["ratio"] == "9:16"
    # The clip rides as a binary, and the json says where to find it.
    ref = item.binary["video"]
    assert ref.mime_type == "video/mp4"
    assert ref.size_bytes == len(MP4)
    assert item.json_["video"]["binary_property"] == "video"
    assert item.json_["video"]["size_bytes"] == len(MP4)


async def test_it_polls_until_the_task_is_terminal() -> None:
    kit = _kit(
        responses=[
            httpx.Response(200, json=task_body("queued")),
            httpx.Response(200, json=task_body("running")),
            httpx.Response(200, json=task_body("succeeded")),
            httpx.Response(200, content=MP4),
        ]
    )
    out = await kit.run()

    (item,) = out["main"]
    assert item.json_["polls"] == 3
    # Three status checks then the download, in that order.
    assert [request.method for request in kit.requests] == ["GET"] * 4
    assert str(kit.requests[0].url).endswith(f"/v2/query/video_generation/{TASK_ID}")
    assert str(kit.requests[3].url) == CLIP_URL


async def test_the_status_check_is_authenticated_and_the_download_is_not() -> None:
    """The bearer token belongs to MiniMax's API, not to a CDN.

    The result URL carries its own signature; sending the workspace's API key
    to whatever host that URL resolves to would hand a secret to a third party
    for no reason.
    """
    kit = _kit()
    await kit.run()

    assert kit.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in kit.requests[1].headers


async def test_a_failed_task_keeps_minimaxs_own_words() -> None:
    kit = _kit(
        responses=[
            httpx.Response(
                200,
                json=task_body(
                    "failed", error={"code": "content_policy", "message": "prompt rejected"}
                ),
            )
        ]
    )
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "content_policy" in str(caught.value)
    assert "prompt rejected" in str(caught.value)
    assert TASK_ID in str(caught.value)


async def test_a_failed_task_with_no_reason_still_names_the_task() -> None:
    kit = _kit(responses=[httpx.Response(200, json=task_body("failed"))])
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "no reason given" in str(caught.value)
    assert TASK_ID in str(caught.value)


async def test_a_cancelled_task_is_terminal_rather_than_waited_on() -> None:
    kit = _kit(responses=[httpx.Response(200, json=task_body("cancelled"))])
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "cancelled" in str(caught.value)
    # It does not claim the money back, because whether there was any depends
    # on how far the task got.
    assert "billed" in str(caught.value)


async def test_running_out_of_time_does_not_claim_the_clip_was_lost() -> None:
    kit = _kit(
        max_wait_seconds=0.02,
        responses=[httpx.Response(200, json=task_body("running")) for _ in range(200)],
    )
    with pytest.raises(MinimaxNotReady) as caught:
        await kit.run()

    message = str(caught.value)
    assert TASK_ID in message
    # The three things an author needs: nothing was cancelled, it will still
    # be billed, and running this again collects it.
    assert "cancelled" in message
    assert "billed" in message
    assert "7 days" in message


async def test_a_succeeded_task_with_no_url_is_named_not_silently_empty() -> None:
    kit = _kit(responses=[httpx.Response(200, json=task_body(url=None))])
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "no download URL" in str(caught.value)
    assert "billed" in str(caught.value)


async def test_a_plain_http_download_url_is_refused() -> None:
    kit = _kit(responses=[httpx.Response(200, json=task_body(url="http://cdn.example/x.mp4"))])
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "non-https" in str(caught.value)


async def test_a_body_that_is_not_a_video_is_refused_rather_than_saved() -> None:
    """The common real failure: an expired-signature page served as a video."""
    kit = _kit(
        responses=[
            httpx.Response(200, json=task_body()),
            httpx.Response(
                200,
                content=b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>",
                headers={"content-type": "video/mp4"},
            ),
        ]
    )
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    message = str(caught.value)
    assert "not a video file" in message
    # It says what it was told, so the lie is visible rather than inferred.
    assert "video/mp4" in message


async def test_webm_is_recognised_too() -> None:
    kit = _kit(responses=[httpx.Response(200, json=task_body()), httpx.Response(200, content=WEBM)])
    out = await kit.run()

    (item,) = out["main"]
    assert item.binary["video"].mime_type == "video/webm"
    assert item.json_["video"]["file_name"].endswith(".webm")


async def test_a_clip_over_the_cap_is_refused_by_name() -> None:
    kit = _kit(
        max_download_megabytes=0.001,  # 1 KB
        responses=[
            httpx.Response(200, json=task_body()),
            httpx.Response(200, content=MP4 + b"\x00" * 4096),
        ],
    )
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    message = str(caught.value)
    assert "larger than" in message
    # It points at the setting the reader can actually change.
    assert "Refuse a clip larger than" in message


async def test_an_expired_download_url_stays_retryable() -> None:
    """A lapsed signature is the one download failure that a re-run fixes."""
    kit = _kit(
        responses=[httpx.Response(200, json=task_body()), httpx.Response(403, content=b"nope")]
    )
    with pytest.raises(MinimaxUnavailable) as caught:
        await kit.run()

    assert "time-limited" in str(caught.value)


async def test_an_unreachable_provider_keeps_its_retry_budget() -> None:
    """The opposite of `minimax_submit`, and deliberately so: a poll creates
    nothing, so there is no duplicate charge for a retry to cause."""

    class Boom(httpx.AsyncClient):
        async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("no route")

    kit = _kit()
    context = kit.context()
    context.http = lambda: Boom()  # type: ignore[method-assign]
    with pytest.raises(MinimaxUnavailable):
        await MinimaxCollectNode().execute(context)


async def test_a_200_without_a_task_is_refused() -> None:
    kit = _kit(responses=[httpx.Response(200, json={"request_id": "trace-1"})])
    with pytest.raises(MinimaxError) as caught:
        await kit.run()

    assert "without a task" in str(caught.value)
    assert "trace-1" in str(caught.value)


async def test_no_task_id_is_a_configuration_error() -> None:
    kit = _kit(task_id="  ")
    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    assert "$json.task_id" in str(caught.value)


async def test_a_clip_is_unpriced_unless_a_rate_is_configured() -> None:
    """MiniMax publishes no per-second USD rate for H3, so the node reports the
    provider's seconds and refuses to invent the money."""
    kit = _kit()
    context = kit.context()
    out = await MinimaxCollectNode().execute(context)

    (item,) = out["main"]
    assert item.json_["priced"] is False
    assert item.json_["cost_usd"] == ""
    # The seconds still travel, so the number is there the moment a rate is.
    assert item.json_["billed_seconds"] == 6.0
    (usage,) = context.usage
    assert usage["cost_usd"] is None
    assert usage["provider"] == "minimax"
    assert usage["model"] == "MiniMax-H3"


async def test_a_configured_rate_prices_the_providers_own_seconds() -> None:
    kit = _kit(price_usd_per_second=0.05)
    context = kit.context()
    out = await MinimaxCollectNode().execute(context)

    (item,) = out["main"]
    assert item.json_["priced"] is True
    # 6 billed seconds, not the 6 that were asked for — they agree here, and
    # `usage.total_seconds` is what is read when they do not.
    assert item.json_["cost_usd"] == "0.30000000"
    (usage,) = context.usage
    assert usage["cost_usd"] == Decimal("0.30000000")


async def test_the_billed_seconds_come_from_usage_not_from_the_request() -> None:
    kit = _kit(
        price_usd_per_second=0.05,
        responses=[
            httpx.Response(
                200,
                json=task_body(duration=6, usage={"total_seconds": 8, "output_seconds": 8}),
            ),
            httpx.Response(200, content=MP4),
        ],
    )
    out = await kit.run()

    (item,) = out["main"]
    assert item.json_["billed_seconds"] == 8.0
    assert item.json_["cost_usd"] == "0.40000000"


async def test_a_negative_rate_is_refused_rather_than_credited() -> None:
    kit = _kit(price_usd_per_second=-1)
    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    assert "cannot be negative" in str(caught.value)


async def test_the_beats_own_json_and_binaries_travel_with_the_clip() -> None:
    """§9's preservation rule, and the reason the Loop body composes: the
    narration generated in Wave 1 has to still be attached when the footage
    arrives."""
    from tamtree_sdk.items import BinaryRef

    narration = BinaryRef(
        id="bin_narration",
        file_name="beat-1.wav",
        mime_type="audio/wav",
        size_bytes=1234,
        storage_key="ws/1/binary/bin_narration",
    )
    kit = _kit(
        inputs=[
            Item.model_validate(
                {
                    "json": {"beat": 1, "narration_text": "A wall of glass.", "task_id": TASK_ID},
                    "binary": {"audio": narration},
                }
            )
        ]
    )
    out = await kit.run()

    (item,) = out["main"]
    assert item.json_["beat"] == 1
    assert item.json_["narration_text"] == "A wall of glass."
    assert item.binary["audio"] is narration
    assert item.binary["video"].mime_type == "video/mp4"


async def test_cancelling_the_run_attempts_the_delete_and_still_cancels() -> None:
    """V2.4's other call site. Cancellation is Temporal's, so it arrives as
    `CancelledError`; the node gets one bounded attempt at the provider DELETE
    and then lets the cancellation through untouched. It does not become a
    failure, and it does not claim the clip stopped."""
    seen: list[tuple[str, str]] = []

    class Cancelling(httpx.AsyncClient):
        async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
            seen.append(("GET", str(url)))
            raise asyncio.CancelledError

        async def delete(self, url: Any, **kwargs: Any) -> httpx.Response:
            seen.append(("DELETE", str(url)))
            return httpx.Response(200, json={"action": "cancelled", "status": "cancelled"})

    kit = _kit()
    context = kit.context()
    context.http = lambda: Cancelling()  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await MinimaxCollectNode().execute(context)

    assert [method for method, _ in seen] == ["GET", "DELETE"]
    assert seen[1][1].endswith(f"/v2/video_generation/{TASK_ID}")


async def test_a_delete_that_fails_does_not_mask_the_cancellation() -> None:
    """A cleanup that raises would turn a cancelled run into a failed one, and
    the run is already ending either way."""

    class Cancelling(httpx.AsyncClient):
        async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
            raise asyncio.CancelledError

        async def delete(self, url: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("gone")

    kit = _kit()
    context = kit.context()
    context.http = lambda: Cancelling()  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await MinimaxCollectNode().execute(context)
