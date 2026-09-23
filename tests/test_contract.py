"""NodeContract conformance — the suite `tamtree plugin test` exists to run.

The base's `test_execute_returns_declared_ports` actually *runs* the node, so
`make_context` has to be a working one: a real key, a real token mint and a
real synthesis answer. That is deliberate — a contract test satisfied by a
node that cannot execute proves only that the manifest parses.
"""

from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item
from tamtree_plugin_sdk.testing import FakeContext, NodeContract, NodeTestKit

from tamtree_shortvideo.credentials import (
    CREDENTIAL_TYPE,
    MINIMAX_CREDENTIAL_TYPE,
    MINIMAX_PRICE_FIELD,
    OPENROUTER_CREDENTIAL_TYPE,
)
from tamtree_shortvideo.google_tts import NODE_NAME, GoogleTtsNode
from tamtree_shortvideo.minimax_cancel import MinimaxCancelNode
from tamtree_shortvideo.minimax_collect import MinimaxCollectNode
from tamtree_shortvideo.minimax_submit import MinimaxSubmitNode
from tamtree_shortvideo.openrouter_tts import NODE_NAME as OPENROUTER_NODE_NAME
from tamtree_shortvideo.openrouter_tts import OpenRouterTtsNode
from tamtree_shortvideo.shot_list import ShotListNode
from tests.audio_fixtures import pcm_bytes, wav_bytes
from tests.conftest import key_file_payload

#: See `tests/test_openrouter.py` for why this has to be captured before any
#: test monkeypatches `openrouter.asyncio.sleep`.
_REAL_SLEEP = asyncio.sleep

TOKEN = "ya29.contract-test-token"


def _responses() -> list[httpx.Response]:
    return [
        httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3599}),
        httpx.Response(
            200,
            json={
                "audioContent": base64.b64encode(wav_bytes(seconds=1.0)).decode(),
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24_000},
            },
        ),
    ]


class TestGoogleTtsContract(NodeContract):
    def make_node(self) -> GoogleTtsNode:
        return GoogleTtsNode()

    def make_context(self) -> FakeContext:
        payload = {"service_account_json": json.dumps(key_file_payload())}
        params: dict[str, Any] = {
            "input_mode": "text",
            "text": "A contract test still has to say something.",
            "language_code": "en-US",
            "audio_encoding": "LINEAR16",
            "price_usd_per_million_chars": 16.0,
            "output_binary_property": "audio",
        }
        return (
            NodeTestKit(GoogleTtsNode())
            .params(**params)
            .credentials({CREDENTIAL_TYPE: payload})
            .responses(_responses())
            .context()
        )


def test_the_node_declares_the_credential_it_cannot_run_without() -> None:
    """Unlike the V0.5 self test, this node reaches a paid API: the palette
    must show the credential slot, and the engine must refuse to dispatch a
    step that has no binding."""
    (requirement,) = GoogleTtsNode().manifest.credentials

    assert requirement.type == CREDENTIAL_TYPE
    assert requirement.required is True


def test_the_manifest_and_the_module_agree_on_the_node_id() -> None:
    assert GoogleTtsNode().manifest.name == NODE_NAME == "shortvideo.google_tts"


class TestMinimaxSubmitContract(NodeContract):
    """The same bar for the second node: `make_context` has to produce one it
    can actually execute against, credential and create answer included."""

    def make_node(self) -> MinimaxSubmitNode:
        return MinimaxSubmitNode()

    def make_context(self) -> FakeContext:
        return (
            NodeTestKit(MinimaxSubmitNode())
            .params(
                model="MiniMax-H3",
                prompt="A coin stack growing in warm light.",
                duration_seconds=6,
                resolution="768P",
                ratio="9:16",
                prompt_expansion_mode="balanced",
            )
            .credentials({MINIMAX_CREDENTIAL_TYPE: {"token": "eyJ-contract-test-key"}})
            .responses([httpx.Response(200, json={"task_id": "t1", "request_id": "r1"})])
            .context()
        )


def test_the_submit_node_declares_its_credential() -> None:
    (requirement,) = MinimaxSubmitNode().manifest.credentials

    assert requirement.type == MINIMAX_CREDENTIAL_TYPE
    assert requirement.required is True


class TestMinimaxCollectContract(NodeContract):
    """Two answers, because collecting is two calls: the status check that
    says `succeeded`, and the download of what it points at. A context that
    stopped at the first would prove the node parses, not that it runs."""

    def make_node(self) -> MinimaxCollectNode:
        return MinimaxCollectNode()

    def make_context(self) -> FakeContext:
        return (
            NodeTestKit(MinimaxCollectNode())
            .params(
                task_id="t1",
                max_wait_seconds=60,
                poll_interval_seconds=0.001,
                max_download_megabytes=1,
                output_binary_property="video",
                price_usd_per_second="",
            )
            .credentials(
                {
                    MINIMAX_CREDENTIAL_TYPE: {
                        "token": "eyJ-contract-test-key",
                        MINIMAX_PRICE_FIELD: "0",
                    }
                }
            )
            .responses(
                [
                    httpx.Response(
                        200,
                        json={
                            "task": {
                                "id": "t1",
                                "model": "MiniMax-H3",
                                "status": "succeeded",
                                "resolution": "768P",
                                "duration": 6,
                                "ratio": "9:16",
                                "content": {"url": "https://cdn.minimax.io/t1.mp4"},
                                "usage": {"total_seconds": 6, "output_seconds": 6},
                            }
                        },
                    ),
                    httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64),
                ]
            )
            .context()
        )


def test_the_collect_node_declares_its_credential() -> None:
    (requirement,) = MinimaxCollectNode().manifest.credentials

    assert requirement.type == MINIMAX_CREDENTIAL_TYPE
    assert requirement.required is True


class TestMinimaxCancelContract(NodeContract):
    def make_node(self) -> MinimaxCancelNode:
        return MinimaxCancelNode()

    def make_context(self) -> FakeContext:
        return (
            NodeTestKit(MinimaxCancelNode())
            .params(task_id="t1", fail_if_not_cancelled=False)
            .credentials({MINIMAX_CREDENTIAL_TYPE: {"token": "eyJ-contract-test-key"}})
            .responses([httpx.Response(200, json={"action": "cancelled", "status": "cancelled"})])
            .context()
        )


def _openrouter_responses(
    *, generation_id: str = "gen-1", cost: float = 0.002
) -> list[httpx.Response]:
    """One `/audio/speech` answer and the generation-cost lookup behind it —
    the two calls `text` mode makes per item."""
    return [
        httpx.Response(
            200,
            content=pcm_bytes(seconds=1.0),
            headers={"X-Generation-Id": generation_id},
        ),
        httpx.Response(
            200,
            json={"data": {"total_cost": cost, "tokens_prompt": 3, "tokens_completion": 40}},
        ),
    ]


class TestOpenRouterTtsContract(NodeContract):
    def make_node(self) -> OpenRouterTtsNode:
        return OpenRouterTtsNode()

    def make_context(self) -> FakeContext:
        params: dict[str, Any] = {
            "input_mode": "text",
            "text": "A contract test still has to say something.",
            "voice": "Zephyr",
            "output_binary_property": "audio",
        }
        return (
            NodeTestKit(OpenRouterTtsNode())
            .params(**params)
            .credentials({OPENROUTER_CREDENTIAL_TYPE: {"token": "sk-or-v1-contract-test"}})
            .responses(_openrouter_responses())
            .context()
        )


def test_the_openrouter_node_declares_the_credential_it_cannot_run_without() -> None:
    (requirement,) = OpenRouterTtsNode().manifest.credentials

    assert requirement.type == OPENROUTER_CREDENTIAL_TYPE
    assert requirement.required is True


def test_the_openrouter_manifest_and_the_module_agree_on_the_node_id() -> None:
    assert OpenRouterTtsNode().manifest.name == OPENROUTER_NODE_NAME == "shortvideo.openrouter_tts"


async def test_openrouter_captions_mode_stitches_exact_per_phrase_timings() -> None:
    """The whole point of the per-phrase-call design: no provider marks, but
    the caption timings are exact because each phrase's duration is measured,
    not estimated — see `openrouter_tts`'s module docstring."""
    kit = (
        NodeTestKit(OpenRouterTtsNode())
        .params(
            input_mode="captions",
            captions=["First line.", "Second line."],
            phrase_gap_seconds=0.1,
            voice="Zephyr",
            output_binary_property="audio",
        )
        .credentials({OPENROUTER_CREDENTIAL_TYPE: {"token": "sk-or-v1-contract-test"}})
        .responses(
            [
                httpx.Response(
                    200, content=pcm_bytes(seconds=1.0), headers={"X-Generation-Id": "gen-1"}
                ),
                httpx.Response(
                    200,
                    json={
                        "data": {"total_cost": 0.001, "tokens_prompt": 2, "tokens_completion": 20}
                    },
                ),
                httpx.Response(
                    200, content=pcm_bytes(seconds=0.5), headers={"X-Generation-Id": "gen-2"}
                ),
                httpx.Response(
                    200,
                    json={
                        "data": {"total_cost": 0.0007, "tokens_prompt": 2, "tokens_completion": 10}
                    },
                ),
            ]
        )
    )
    outputs = await kit.run()
    (item,) = outputs["main"]
    data = item.json_

    assert data["duration_seconds"] == pytest.approx(1.6)  # 1.0 + 0.1 gap + 0.5
    assert [mark["time_seconds"] for mark in data["marks"]] == pytest.approx([0.0, 1.1])
    assert data["captions"][0]["end_seconds"] == pytest.approx(1.0)
    assert data["captions"][1]["start_seconds"] == pytest.approx(1.1)
    assert data["captions"][1]["end_seconds"] == pytest.approx(1.6)
    assert data["usage"]["calls"] == 2
    assert data["priced"] is True
    assert data["cost_usd"] == str(Decimal("0.001") + Decimal("0.0007"))


async def test_openrouter_reports_unpriced_when_the_ledger_never_catches_up(monkeypatch) -> None:
    """A ledger that never resolves must not silently under-report as zero —
    the call is flagged unpriced, matching `minimax_collect`'s posture."""
    monkeypatch.setattr(
        "tamtree_shortvideo.openrouter.asyncio.sleep", lambda seconds: _REAL_SLEEP(0)
    )
    kit = (
        NodeTestKit(OpenRouterTtsNode())
        .params(input_mode="text", text="Whatever the ledger says later.", voice="Zephyr")
        .credentials({OPENROUTER_CREDENTIAL_TYPE: {"token": "sk-or-v1-contract-test"}})
        .responses(
            [
                httpx.Response(
                    200, content=pcm_bytes(seconds=1.0), headers={"X-Generation-Id": "gen-1"}
                ),
                *([httpx.Response(404)] * 4),  # exhausts the bounded retry
            ]
        )
    )
    outputs = await kit.run()
    (item,) = outputs["main"]
    data = item.json_

    assert data["priced"] is False
    assert data["cost_usd"] == ""


class TestShotListContract(NodeContract):
    """No credential and no socket — a pure check on a script — but it still
    has to honour the node contract the engine runs every step under."""

    def make_node(self) -> ShotListNode:
        return ShotListNode()

    def make_context(self) -> FakeContext:
        script = {"beats": [{"narration": "One line.", "visual_prompt": "A calm sea."}]}
        return (
            NodeTestKit(ShotListNode())
            .inputs("main", [Item.model_validate({"json": {"text": json.dumps(script)}})])
            .context()
        )
