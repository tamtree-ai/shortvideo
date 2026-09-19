"""Boot-time discovery: what the platform reads before anything runs.

V0.5's whole point. These assertions are about *packaging*, not behaviour —
they are what make "installs on Tamtree and works" a checked claim rather than
a hope.
"""

from importlib.metadata import EntryPoint
from importlib.resources import files

import pytest
from tamtree_plugin_sdk import CONTRACTS_VERSION, GROUP_NODES, PluginRegistry
from tamtree_sdk import PluginRefusedError

from tamtree_shortvideo import NODES
from tamtree_shortvideo.nodes import CATEGORY, ICON, NODE_NAME

EXPECTED_NODES = {"shortvideo.selftest"}

PLUGIN_NAME = "shortvideo"


def _entry_points() -> list[EntryPoint]:
    return [EntryPoint(name=PLUGIN_NAME, value="tamtree_shortvideo:NODES", group=GROUP_NODES)]


def test_every_node_name_matches_its_manifest() -> None:
    assert {node.manifest.name for node in NODES} == EXPECTED_NODES
    for node in NODES:
        assert node.name == node.manifest.name
        assert node.manifest.icon == ICON
        assert node.manifest.category == CATEGORY


def test_node_ids_stay_out_of_the_first_party_namespace() -> None:
    """`tamtree.*` is reserved for in-tree nodes (plan D11/§5.6).

    Every id this plugin publishes carries its own prefix, so an install can
    never shadow or be shadowed by a core node.
    """
    for node in NODES:
        assert node.manifest.name.startswith("shortvideo.")
        assert not node.manifest.name.startswith("tamtree.")


def test_nodes_declare_an_output_schema() -> None:
    """Declared provenance, so the editor offers the fields downstream before
    the flow has ever run."""
    for node in NODES:
        schema = node.manifest.outputs[0].output_schema
        assert schema is not None, node.manifest.name
        assert "ok" in schema["properties"]


def test_skeleton_node_needs_no_credential() -> None:
    """A fresh install must be able to run this node with nothing configured —
    that is what makes it usable as an install check."""
    assert [node.manifest.credentials for node in NODES] == [[]]


def test_ships_a_square_icon() -> None:
    svg = (files("tamtree_shortvideo") / ICON).read_bytes()
    assert len(svg) < 20_000
    assert b"<svg" in svg
    assert b'viewBox="0 0 24 24"' in svg


def test_plugin_discovers_its_nodes() -> None:
    registry = PluginRegistry()
    registry.discover(_entry_points)

    assert EXPECTED_NODES <= set(registry.nodes())
    assert registry.node_plugins()[NODE_NAME] == PLUGIN_NAME

    plugin = registry.plugins()[PLUGIN_NAME]
    assert "node" in plugin.manifest.kinds
    assert plugin.manifest.contracts.sdk == "^1.33"


def test_claims_no_permission_it_does_not_use() -> None:
    """The skeleton talks to nothing. Waves 1-2 add `network`/`secrets` with
    the nodes that need them; until then an empty declaration is the honest
    one and this test is what stops it drifting open by accident."""
    registry = PluginRegistry()
    registry.discover(_entry_points)

    capabilities = registry.plugins()[PLUGIN_NAME].manifest.capabilities
    assert capabilities.permissions == []
    assert capabilities.egress_allowlist == []


def test_an_older_instance_refuses_the_plugin_at_boot() -> None:
    """§20.3: the contracts pin is a boot gate, not documentation.

    An instance below the pinned floor must refuse the plugin outright rather
    than load it and fail later inside a node.
    """
    registry = PluginRegistry(contracts_version="1.32.0")
    with pytest.raises(PluginRefusedError, match="refused at boot"):
        registry.discover(_entry_points)


def test_the_host_this_is_built_against_satisfies_the_pin() -> None:
    """The mirror of the test above: the pin must not lock out the very SDK
    the plugin ships against."""
    registry = PluginRegistry(contracts_version=CONTRACTS_VERSION)
    registry.discover(_entry_points)
    assert EXPECTED_NODES <= set(registry.nodes())
