"""The two shipped flows, checked against the nodes they actually call.

A flow YAML is the one artifact in this repo that no compiler reads. Rename a
param in `minimax_collect.py` and nothing here fails — the flow just stops
working, at install time, on somebody else's instance. So these tests parse
both files with the engine's own `FlowDefinition` model and then hold every
`shortvideo.*` step against the manifest it names: the node exists, the params
exist, and the credential slot is one the node actually asks for.

What they deliberately do not check is behaviour. `tamtree validate` needs a
live instance to resolve node pins against, and nothing here has ever run
against one (no keys — see the handover). These are structural tests, and the
distinction is worth keeping visible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tamtree_sdk.flowdef import FlowDefinition

from tamtree_shortvideo import NODES

FLOWS = Path(__file__).resolve().parent.parent / "flows"
BODY = "generate-one-beat"
PARENT = "beats-to-clips"

MANIFESTS = {node.manifest.name: node.manifest for node in NODES}


def _document(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((FLOWS / f"{name}.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _definition(name: str) -> FlowDefinition:
    return FlowDefinition.model_validate(_document(name)["spec"])


@pytest.mark.parametrize("name", [BODY, PARENT])
def test_the_flow_parses_as_the_engine_would_read_it(name: str) -> None:
    """`extra="forbid"` all the way down, so a typo in a key is an error here
    rather than a param silently ignored on a real instance."""
    document = _document(name)

    assert document["apiVersion"] == "tamtree.dev/v1"
    assert document["kind"] == "Flow"
    assert document["metadata"]["name"] == name
    definition = _definition(name)
    assert definition.flow_type == "pipeline"


@pytest.mark.parametrize("name", [BODY, PARENT])
def test_every_node_is_reachable_and_ordered(name: str) -> None:
    definition = _definition(name)
    ids = [node.id for node in definition.nodes]

    # `nodes_in_order` is stored, never derived at run time (§6.3), so a node
    # missing from it is a node the engine will not run.
    assert sorted(definition.nodes_in_order) == sorted(ids)
    assert len(set(ids)) == len(ids)
    for connection in definition.connections:
        assert connection.from_node in ids
        assert connection.to_node in ids
        # Order is the flow's contract: a connection may only point forward.
        assert definition.nodes_in_order.index(connection.from_node) < (
            definition.nodes_in_order.index(connection.to_node)
        )


@pytest.mark.parametrize("name", [BODY, PARENT])
def test_this_plugins_steps_name_params_the_nodes_actually_have(name: str) -> None:
    """The drift this file exists to catch."""
    definition = _definition(name)
    ours = [node for node in definition.nodes if node.type.startswith("shortvideo.")]

    for node in ours:
        assert node.type in MANIFESTS, f"{name}: no such node {node.type}"
        manifest = MANIFESTS[node.type]
        declared = {param.name for param in manifest.params}
        unknown = set(node.params) - declared
        assert not unknown, f"{name}/{node.id}: {node.type} has no param(s) {sorted(unknown)}"


@pytest.mark.parametrize("name", [BODY, PARENT])
def test_this_plugins_steps_bind_the_credential_the_node_requires(name: str) -> None:
    definition = _definition(name)

    for node in definition.nodes:
        if not node.type.startswith("shortvideo."):
            continue
        required = {
            requirement.type
            for requirement in MANIFESTS[node.type].credentials
            if requirement.required
        }
        assert set(node.credentials) >= required, (
            f"{name}/{node.id}: {node.type} needs {sorted(required)}"
        )


def test_the_body_starts_with_a_subworkflow_trigger() -> None:
    """A Loop refuses a body that does not, and it refuses it at plan time on
    the importer's instance — long after this repo could have said so."""
    definition = _definition(BODY)

    first = definition.nodes_in_order[0]
    entry = next(node for node in definition.nodes if node.id == first)
    assert entry.type == "tamtree.subworkflow_trigger"
    schema = entry.params["input_schema"]
    # The two fields the body cannot work without, and the one that must not be
    # inferred from position: `on_item_error: skip` shifts every later index.
    assert set(schema["required"]) == {"beat_number", "visual_prompt"}


def test_the_parent_runs_this_body_one_beat_at_a_time() -> None:
    definition = _definition(PARENT)
    loop = next(node for node in definition.nodes if node.type == "tamtree.loop")

    assert loop.params["flow_id"] == BODY
    assert loop.params["mode"] == "for_each"
    # One pass is one beat: one step budget, one retry scope, one failure.
    assert loop.params["batch_size"] == 1


def test_a_failed_beat_does_not_discard_its_successful_siblings() -> None:
    """V2.6's requirement, stated as the test that would catch its removal.

    Every beat that already succeeded has been paid for. `stop` would throw
    those clips away because a later one was refused.
    """
    definition = _definition(PARENT)
    loop = next(node for node in definition.nodes if node.type == "tamtree.loop")

    assert loop.params["on_item_error"] == "skip"
    # And the failures have somewhere to go: `main` drops a skipped pass
    # entirely, so without the `status` port "which beat is missing, and why"
    # is not answerable downstream.
    ports = {
        connection.from_port
        for connection in definition.connections
        if connection.from_node == loop.id
    }
    assert "status" in ports


def test_a_shot_list_longer_than_the_cap_stops_rather_than_half_running() -> None:
    """For a paid step, quietly doing part of the job is the worse failure."""
    definition = _definition(PARENT)
    loop = next(node for node in definition.nodes if node.type == "tamtree.loop")

    assert loop.params["on_max_iterations"] == "fail"


def test_the_submit_step_may_not_retry_and_the_collect_step_must() -> None:
    """The asymmetry the whole two-node split exists for, asserted where an
    author would actually break it — in the flow, not in the node.

    A create has no idempotency key, so a retried submit is a second charge.
    A poll creates nothing, so retrying it cannot double-charge anything.
    """
    definition = _definition(BODY)
    by_id = {node.id: node for node in definition.nodes}

    assert by_id["submit"].settings.retry_on_fail is False
    assert by_id["collect"].settings.retry_on_fail is True


def test_the_step_timeout_outlives_the_nodes_own_wait() -> None:
    """Otherwise the step is killed from outside while the node still believes
    it has time left, and the author never sees the node's own message — that
    nothing was cancelled, the clip is still billing, and re-running collects
    it."""
    definition = _definition(BODY)
    collect = next(node for node in definition.nodes if node.id == "collect")

    assert collect.settings.timeout_s > collect.params["max_wait_seconds"]
