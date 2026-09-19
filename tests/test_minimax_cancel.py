"""`shortvideo.minimax_cancel` — three outcomes wearing one verb.

MiniMax's `DELETE` cancels a queued task, deletes a finished one, and refuses a
running one. V2.4's requirement is not that the node cancel things; it is that
the node never claim to have cancelled something it did not. Every test here is
about that distinction, because it is the difference between a bill the user
expected and one they did not.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import NodeTestKit

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE
from tamtree_shortvideo.minimax import MinimaxUnavailable
from tamtree_shortvideo.minimax_cancel import RUNNING_NOTE, MinimaxCancelNode

TOKEN = "eyJ-a-real-looking-minimax-key"
TASK_ID = "video_task_01H9Z"
REQUEST_ID = "trace-abc-123"


def _kit(
    *,
    responses: list[httpx.Response] | None = None,
    inputs: list[Item] | None = None,
    **overrides: Any,
) -> NodeTestKit:
    kit = (
        NodeTestKit(MinimaxCancelNode())
        .params(**{"task_id": TASK_ID, "fail_if_not_cancelled": False, **overrides})
        .credentials({MINIMAX_CREDENTIAL_TYPE: {"token": TOKEN}})
        .responses(
            responses
            or [
                httpx.Response(
                    200, json={"task_id": TASK_ID, "action": "cancelled", "status": "cancelled"}
                )
            ]
        )
    )
    if inputs is not None:
        kit.inputs("main", inputs)
    return kit


# -- the three outcomes ------------------------------------------------------


async def test_a_queued_task_is_cancelled_and_says_it_was_not_charged() -> None:
    """MiniMax documents no charge for a task cancelled before it started,
    which is the one case where "cancelled" is the whole truth."""
    kit = _kit()

    (item,) = (await kit.run())["main"]

    assert item.json_["cancelled"] is True
    assert item.json_["action"] == "cancelled"
    assert "does not charge" in item.json_["note"]


async def test_a_running_task_gets_the_exact_sentence_v2_4_asks_for() -> None:
    """The load-bearing test. MiniMax refuses to cancel a running task; there
    is no forced stop, and the generation keeps billing."""
    kit = _kit(
        responses=[
            httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_state", "message": "cannot cancel while processing"},
                    "request_id": REQUEST_ID,
                },
            )
        ]
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["cancelled"] is False
    assert item.json_["note"] == RUNNING_NOTE
    assert item.json_["note"] == (
        "local wait stopped; provider generation may continue and may be billed"
    )
    assert item.json_["request_id"] == REQUEST_ID


async def test_a_finished_task_is_deleted_not_cancelled_and_the_note_says_so() -> None:
    """`DELETE` on a succeeded task removes it. Reporting that as "cancelled"
    would claim spend was avoided that had already happened."""
    kit = _kit(
        responses=[
            httpx.Response(200, json={"task_id": TASK_ID, "action": "deleted", "status": "deleted"})
        ]
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["cancelled"] is False
    assert item.json_["action"] == "deleted"
    assert "was billed" in item.json_["note"]


async def test_it_calls_delete_on_the_v2_path() -> None:
    kit = _kit()

    await kit.run()

    request = kit.requests[0]
    assert request.method == "DELETE"
    assert str(request.url) == f"https://api.minimax.io/v2/video_generation/{TASK_ID}"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


# -- failing loudly, but only when asked -------------------------------------


async def test_a_refusal_does_not_fail_the_step_by_default() -> None:
    """This node's natural home is a cleanup path, and a cleanup step that
    throws because the clip was already running turns one problem into two."""
    kit = _kit(responses=[httpx.Response(400, json={"error": {"message": "running"}})])

    output = await kit.run()

    assert output["main"][0].json_["cancelled"] is False


async def test_strict_mode_turns_a_refusal_into_a_failed_step() -> None:
    kit = _kit(
        fail_if_not_cancelled=True,
        responses=[httpx.Response(400, json={"error": {"message": "running"}})],
    )

    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    assert RUNNING_NOTE in str(caught.value)


async def test_strict_mode_does_not_fire_on_a_real_cancellation() -> None:
    kit = _kit(fail_if_not_cancelled=True)

    (item,) = (await kit.run())["main"]

    assert item.json_["cancelled"] is True


# -- the rest ----------------------------------------------------------------


async def test_a_missing_task_id_says_where_to_map_it_from() -> None:
    kit = _kit(task_id="")

    with pytest.raises(NodeConfigurationError, match=r"\$json.task_id"):
        await kit.run()

    assert kit.requests == []


async def test_a_server_error_keeps_its_retry_budget() -> None:
    """Unlike a create, a second DELETE is free and idempotent — the worst it
    does is find the task already gone."""
    kit = _kit(responses=[httpx.Response(503, json={"request_id": REQUEST_ID})])

    with pytest.raises(MinimaxUnavailable) as caught:
        await kit.run()

    assert not isinstance(caught.value, NodeConfigurationError)
    assert REQUEST_ID in str(caught.value)


async def test_a_dropped_connection_keeps_its_retry_budget() -> None:
    kit = _kit()
    ctx = kit.context()
    ctx.http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("reset"))
        )
    )

    with pytest.raises(MinimaxUnavailable, match="another attempt"):
        await MinimaxCancelNode().execute(ctx)


async def test_the_source_json_and_attachments_survive() -> None:
    beat = Item.model_validate(
        {
            "json": {"beat": 3, "task_id": TASK_ID},
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

    assert item.json_["beat"] == 3
    assert item.binary is not None
    assert "audio" in item.binary


async def test_one_cancellation_per_input_item() -> None:
    kit = _kit(
        responses=[
            httpx.Response(200, json={"action": "cancelled", "status": "cancelled"}),
            httpx.Response(400, json={"error": {"message": "running"}}),
        ],
        inputs=[
            Item.model_validate({"json": {"beat": 1}}),
            Item.model_validate({"json": {"beat": 2}}),
        ],
    )

    output = await kit.run()

    assert [item.json_["cancelled"] for item in output["main"]] == [True, False]


async def test_the_api_key_never_reaches_the_output_or_an_error() -> None:
    kit = _kit(responses=[httpx.Response(503, json={})])

    with pytest.raises(MinimaxUnavailable) as caught:
        await kit.run()

    assert TOKEN not in str(caught.value)
    assert TOKEN not in json.dumps((await _kit().run())["main"][0].json_)
