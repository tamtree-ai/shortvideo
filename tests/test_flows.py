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

FLOWS = Path(__file__).resolve().parent.parent / "template" / "flows"
BODY = "generate-one-beat"
PARENT = "beats-to-clips"
TEMPLATE = "short-form-video"
ALL = [BODY, PARENT, TEMPLATE]

MANIFESTS = {node.manifest.name: node.manifest for node in NODES}


def _document(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((FLOWS / f"{name}.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _definition(name: str) -> FlowDefinition:
    return FlowDefinition.model_validate(_document(name)["spec"])


@pytest.mark.parametrize("name", ALL)
def test_the_flow_parses_as_the_engine_would_read_it(name: str) -> None:
    """`extra="forbid"` all the way down, so a typo in a key is an error here
    rather than a param silently ignored on a real instance."""
    document = _document(name)

    assert document["apiVersion"] == "tamtree.dev/v1"
    assert document["kind"] == "Flow"
    assert document["metadata"]["name"] == name
    definition = _definition(name)
    assert definition.flow_type == "pipeline"


@pytest.mark.parametrize("name", ALL)
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


@pytest.mark.parametrize("name", ALL)
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


@pytest.mark.parametrize("name", ALL)
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


# --- the Short-form video template (V4.2) -------------------------------------


def _by_id(name: str) -> dict[str, Any]:
    return {node.id: node for node in _definition(name).nodes}


def _into(name: str, node_id: str) -> set[tuple[str, str, str]]:
    return {
        (c.from_node, c.from_port, c.to_port)
        for c in _definition(name).connections
        if c.to_node == node_id
    }


def test_the_template_checks_the_script_before_anything_is_paid_for() -> None:
    """The shot list sits between the model and the first paid call, and its
    beat ceiling is under the loop's — so an oversized plan is refused before
    the narration is bought, not after."""
    definition = _definition(TEMPLATE)
    order = definition.nodes_in_order
    nodes = _by_id(TEMPLATE)

    assert order.index("shot_list") < order.index("narrate") < order.index("per_beat")
    assert nodes["shot_list"].params["max_beats"] <= nodes["per_beat"].params["max_iterations"]


def test_the_template_narrates_the_shot_list_s_marked_phrases() -> None:
    narrate = _by_id(TEMPLATE)["narrate"]

    # Phrase-list mode is what produces a `beat-<N>` mark per phrase; plain
    # text would leave assemble with no beat boundaries.
    assert narrate.params["input_mode"] == "captions"
    assert "phrases" in narrate.params["captions"]


def test_the_template_runs_the_same_loop_body_under_the_same_policy() -> None:
    loop = _by_id(TEMPLATE)["per_beat"]

    assert loop.params["flow_id"] == BODY
    assert loop.params["batch_size"] == 1
    assert loop.params["on_item_error"] == "skip"
    assert loop.params["on_max_iterations"] == "fail"


def test_assemble_gets_clips_on_main_and_the_narration_on_its_own_port() -> None:
    assert _into(TEMPLATE, "assemble") == {
        ("per_beat", "main", "main"),
        ("narrate", "main", "narration"),
        ("reuse_narration", "found", "narration"),
    }


def test_the_final_render_follows_approval_and_is_pinned_to_the_draft() -> None:
    """The approve-one-render-another guard, asserted where an author would
    break it: the final compose is reachable only from `approved`, and it
    refuses a timeline whose digest is not the draft's."""
    nodes = _by_id(TEMPLATE)

    assert nodes["draft"].params["draft"] is True
    assert nodes["final"].params["draft"] is False
    assert _into(TEMPLATE, "final") == {("review", "approved", "main")}
    assert "$node('draft')" in nodes["final"].params["expected_digest"]


def test_a_rejection_ends_the_run_and_regenerates_nothing() -> None:
    """No automatic paid regeneration on rejection (V4.4): nothing downstream
    of `rejected` is a provider node."""
    definition = _definition(TEMPLATE)
    downstream = {
        c.to_node
        for c in definition.connections
        if c.from_node == "review" and c.from_port == "rejected"
    }
    types = {node.id: node.type for node in definition.nodes}

    assert downstream == {"rejected"}
    # What a replay needs survives the rejection.
    fields = _by_id(TEMPLATE)["rejected"].params["fields"]
    assert "_approval" in fields["review_note"]
    assert "shot_list" in fields["script"]
    assert not any(c.from_node == "rejected" for c in definition.connections), (
        "a rejection must be terminal"
    )
    assert types["rejected"] == "tamtree.set"


def test_the_approval_waits_and_silence_rejects() -> None:
    review = _by_id(TEMPLATE)["review"]

    assert review.type == "tamtree.approval"
    assert review.params["on_timeout"] == "reject"


def test_the_reviewer_watches_the_draft_and_the_audit_names_its_timeline() -> None:
    """V4.3: the gate previews the rendered draft — not the clips and
    narration riding the same item — and records the digest the final render
    is pinned to."""
    review = _by_id(TEMPLATE)["review"]

    assert review.params["preview"] == _by_id(TEMPLATE)["draft"].params["attachment"]
    assert "digest" in review.params["subject"]


def test_the_template_ships_editor_test_data_for_its_trigger() -> None:
    definition = _definition(TEMPLATE)

    (item,) = definition.pinned_data["topic"]
    assert item.json_["topic"]


# --- V4.4: recovery is a re-run that buys nothing twice ------------------------

#: The submit params that change a generated clip. Every one must be in the
#: reuse key with the same value, or a replay would hand back a clip made
#: differently from the one the flow asks for.
CLIP_INPUTS = ("model", "duration_seconds", "resolution", "ratio")
#: The narration params that change the audio.
VOICE_INPUTS = ("voice", "phrase_gap_seconds")


def test_the_clip_reuse_key_is_exactly_what_submit_sends() -> None:
    nodes = _by_id(BODY)
    key = nodes["reuse_clip"].params["key"]
    submit = nodes["submit"].params

    for name in CLIP_INPUTS:
        assert key[name] == submit[name], f"reuse key {name!r} drifted from submit"
    assert key["prompt"] == submit["prompt"]
    # And the key names nothing submit does not send.
    assert set(key) == {*CLIP_INPUTS, "prompt"}


def test_a_found_clip_never_reaches_the_paid_step() -> None:
    """The whole V4.4 promise, stated structurally: submit is reachable only
    from `missing`, and the found branch ends in its own Return."""
    assert _into(BODY, "submit") == {("reuse_clip", "missing", "main")}
    assert _into(BODY, "label_reused") == {("reuse_clip", "found", "main")}
    nodes = _by_id(BODY)
    assert nodes["reused"].type == "tamtree.return"
    assert nodes["generated"].type == "tamtree.return"
    # `failed` is the one status a Loop treats as a failed pass.
    assert nodes["reused"].params["status"] != "failed"


def test_a_generated_clip_is_saved_under_the_name_the_next_run_looks_up() -> None:
    save = _by_id(BODY)["save_clip"]

    assert "asset_name" in save.params["name"]
    assert set(save.params["metadata"]["fields"]) >= {"duration_seconds", "task_id"}


def test_a_reused_clip_is_labelled_like_a_generated_one() -> None:
    """The parent cannot tell the two apart except by `reused` — assemble
    reads the same fields either way."""
    nodes = _by_id(BODY)
    generated = set(nodes["label_clip"].params["fields"])
    reused = set(nodes["label_reused"].params["fields"])

    assert generated == reused
    assert {"beat_number", "asset_id", "duration_seconds"} <= reused


def test_the_narration_reuse_key_is_exactly_what_narrate_uses() -> None:
    nodes = _by_id(TEMPLATE)
    key = nodes["reuse_narration"].params["key"]
    narrate = nodes["narrate"].params

    for name in VOICE_INPUTS:
        assert key[name] == narrate[name], f"reuse key {name!r} drifted from narrate"
    assert key["phrases"] == narrate["captions"]
    assert _into(TEMPLATE, "narrate") == {("reuse_narration", "missing", "main")}


def test_saved_narration_keeps_what_assemble_needs_to_skip_synthesis() -> None:
    save = _by_id(TEMPLATE)["save_narration"]

    assert "asset_name" in save.params["name"]
    assert set(save.params["metadata"]["fields"]) >= {"duration_seconds", "captions", "audio"}


def test_a_replay_s_script_skips_the_model() -> None:
    assert _into(TEMPLATE, "script") == {("has_script", "false", "main")}
    assert ("has_script", "true", "main") in _into(TEMPLATE, "shot_list")
    assert "script" in _by_id(TEMPLATE)["shot_list"].params["script"]
