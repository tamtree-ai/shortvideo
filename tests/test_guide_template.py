"""The `guide-video` template package: structure, and its two code steps run.

Like `test_flows.py`, the flow is parsed with the engine's own `FlowDefinition`
and every `shortvideo.*` step is held against its manifest. The two
`tamtree.code` steps are the only logic the flow carries, so they are executed
here on the shapes the steps before them produce — a written guide on the
trigger, then the narration step's output — rather than only parsed.
`browser_capture.capture` belongs to another distribution, so its params are
checked by name against what that node declares, written out below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tamtree_kernel.flowdef import FlowDefinition
from tamtree_kernel.resources import TemplateManifest

from tamtree_shortvideo import NODES
from tamtree_shortvideo.guide_assemble import NARRATION_PORT

PACKAGE = Path(__file__).resolve().parent.parent / "guide-template"
MANIFESTS = {node.manifest.name: node.manifest for node in NODES}
#: What `browser_capture.capture` declares (browser-capture `plugin/`, B3).
CAPTURE_PARAMS = {"scene", "attachment", "timeout_s"}
CAPTURE_CREDENTIALS = {"browser_capture_mask", "browser_session"}

GUIDE = {
    "title": "HTTP Request",
    "sections": [
        {
            "id": "add-step",
            "title": "Add an HTTP Request step",
            "lines": ["Open the workflow.", "Add an HTTP Request step after the trigger."],
            "steps": [{"action": "goto", "path": "/flows/x/edit"}],
            "callouts": [{"step": 0, "text": "The editor"}],
        },
        {
            "id": "inspect",
            "title": "Inspect the output",
            "lines": ["Run it and open the output tab."],
            "steps": [{"action": "click", "target": {"text": "Output"}}],
        },
    ],
}


def _manifest() -> TemplateManifest:
    return TemplateManifest.model_validate(yaml.safe_load((PACKAGE / "template.yaml").read_text()))


def _flow() -> FlowDefinition:
    (path,) = _manifest().spec.resources
    document = yaml.safe_load((PACKAGE / path).read_text())
    assert document["kind"] == "Flow"
    assert document["metadata"]["name"] == "guide-video"
    return FlowDefinition.model_validate(document["spec"])


def _node(node_id: str) -> Any:
    return next(node for node in _flow().nodes if node.id == node_id)


def _run(node_id: str, item_json: dict[str, Any]) -> dict[str, Any]:
    """Execute a `tamtree.code` step the way its protocol does: `items` in,
    `items` out."""
    scope: dict[str, Any] = {"items": [{"json": item_json}]}
    exec(_node(node_id).params["code"], scope)  # noqa: S102 - the flow's own code
    (out,) = scope["items"]
    return out["json"]


def test_the_package_validates_and_every_node_is_ordered() -> None:
    flow = _flow()
    ids = [node.id for node in flow.nodes]
    assert sorted(flow.nodes_in_order) == sorted(ids)
    for connection in flow.connections:
        assert flow.nodes_in_order.index(connection.from_node) < flow.nodes_in_order.index(
            connection.to_node
        )


def test_every_plugin_step_uses_params_and_credentials_its_node_has() -> None:
    required = {r.name for r in _manifest().spec.requires.plugins}
    slots = {c.slot: c.type for c in _manifest().spec.requires.credentials}
    for node in _flow().nodes:
        if node.type.startswith("shortvideo."):
            assert "shortvideo" in required
            manifest = MANIFESTS[node.type]
            assert set(node.params) <= {p.name for p in manifest.params}, node.id
        elif node.type == "browser_capture.capture":
            assert "browser-capture" in required
            assert set(node.params) <= CAPTURE_PARAMS
            assert set(node.credentials) <= CAPTURE_CREDENTIALS
        else:
            assert node.type.startswith("tamtree."), node.type
        for credential_type, slot in (node.credentials or {}).items():
            assert slots[slot] == credential_type, node.id


def test_the_recordings_reach_main_and_the_narration_its_own_port() -> None:
    into = [c for c in _flow().connections if c.to_node == "assemble"]
    assert {(c.from_node, c.to_port or "main") for c in into} == {
        ("capture", "main"),
        ("narrate", NARRATION_PORT),
    }


def test_the_final_render_follows_approval_and_is_pinned_to_the_draft() -> None:
    final = _node("final")
    assert final.params["expected_digest"] == "{{ $node('draft').items[0].json['digest'] }}"
    assert any(
        c.from_node == "review" and c.from_port == "approved" and c.to_node == "final"
        for c in _flow().connections
    )


def test_prepare_names_each_phrase_after_its_section() -> None:
    out = _run("prepare", {"brief": "x", "guide": GUIDE})

    assert [p["name"] for p in out["phrases"]] == ["add-step.1", "add-step.2", "inspect.1"]
    assert out["sections"][0] == {
        "id": "add-step",
        "title": "Add an HTTP Request step",
        "callouts": [{"step": 0, "text": "The editor"}],
    }
    assert out["steps"]["inspect"] == [{"action": "click", "target": {"text": "Output"}}]


def test_prepare_reads_the_model_s_reply_however_it_is_wrapped() -> None:
    import json

    reply = f"Here is the guide:\n```json\n{json.dumps(GUIDE)}\n```"
    assert len(_run("prepare", {"text": reply})["phrases"]) == 3

    with pytest.raises(ValueError, match="holds no JSON guide"):
        _run("prepare", {"text": "Sorry, I can't."})


def test_prepare_refuses_an_id_the_capture_would_refuse() -> None:
    bad = {"sections": [{**GUIDE["sections"][0], "id": "Add Step"}]}
    with pytest.raises(ValueError, match="must be lowercase"):
        _run("prepare", {"guide": bad})


def test_each_scene_lasts_at_least_as_long_as_its_section_is_spoken() -> None:
    prepared = _run("prepare", {"guide": GUIDE})
    narrated = {
        **prepared,
        "base_url": "http://localhost:5173",
        "duration_seconds": 9.0,
        "captions": [
            {"name": "add-step.1", "start_seconds": 0.2, "end_seconds": 2.0},
            {"name": "add-step.2", "start_seconds": 2.25, "end_seconds": 5.0},
            {"name": "inspect.1", "start_seconds": 5.25, "end_seconds": 9.0},
        ],
    }

    scene = _run("scene", narrated)["scene"]

    assert scene["version"] == 1 and scene["base_url"] == "http://localhost:5173"
    # Section 0's beat starts at frame 0, so its recording covers the lead-in.
    assert [(s["id"], s["min_duration_ms"]) for s in scene["scenes"]] == [
        ("add-step", 5750),
        ("inspect", 4250),
    ]
    assert scene["scenes"][1]["steps"] == GUIDE["sections"][1]["steps"]
