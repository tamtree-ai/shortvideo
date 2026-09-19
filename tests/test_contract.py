"""NodeContract conformance — the suite `tamtree plugin test` exists to run.

The base's `test_execute_returns_declared_ports` actually *runs* the node, so
`make_context` has to be a working one: a real key, a real token mint and a
real synthesis answer. That is deliberate — a contract test satisfied by a
node that cannot execute proves only that the manifest parses.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from tamtree_plugin_sdk.testing import FakeContext, NodeContract, NodeTestKit

from tamtree_shortvideo.credentials import CREDENTIAL_TYPE
from tamtree_shortvideo.google_tts import NODE_NAME, GoogleTtsNode
from tests.audio_fixtures import wav_bytes
from tests.conftest import key_file_payload

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
