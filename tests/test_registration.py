"""Boot-time discovery: what the platform reads before anything runs.

V0.5's whole point. These assertions are about *packaging*, not behaviour —
they are what make "installs on Tamtree and works" a checked claim rather than
a hope.
"""

from importlib.metadata import EntryPoint
from importlib.resources import files

import pytest
from tamtree_plugin_sdk import (
    CONTRACTS_VERSION,
    GROUP_CREDENTIAL_TYPES,
    GROUP_NODES,
    PluginRegistry,
)
from tamtree_sdk import PluginRefusedError

from tamtree_shortvideo import NODES
from tamtree_shortvideo.credentials import (
    CREDENTIAL_TYPE,
    MINIMAX_CREDENTIAL_TYPE,
    OPENROUTER_CREDENTIAL_TYPE,
)
from tamtree_shortvideo.google_auth import DEFAULT_TOKEN_URI
from tamtree_shortvideo.google_tts import NODE_NAME, SYNTHESIZE_URL
from tamtree_shortvideo.minimax import API_HOST
from tamtree_shortvideo.nodes import CATEGORY, ICON
from tamtree_shortvideo.openrouter import API_HOST as OPENROUTER_API_HOST

EXPECTED_NODES = {
    "shortvideo.google_tts",
    "shortvideo.openrouter_tts",
    "shortvideo.minimax_submit",
    "shortvideo.minimax_collect",
    "shortvideo.minimax_cancel",
    "shortvideo.openrouter_video_submit",
    "shortvideo.openrouter_video_collect",
    "shortvideo.compose",
    "shortvideo.shot_list",
    "shortvideo.assemble",
    "shortvideo.reuse",
}

EXPECTED_CREDENTIAL_TYPES = {"google_service_account", "minimax_api", OPENROUTER_CREDENTIAL_TYPE}

PLUGIN_NAME = "shortvideo"


def _entry_points() -> list[EntryPoint]:
    """Every contribution this distribution makes, exactly as `pyproject.toml`
    declares it — a test that sighted only one group would not notice a second
    one that refuses the boot."""
    return [
        EntryPoint(name=PLUGIN_NAME, value="tamtree_shortvideo:NODES", group=GROUP_NODES),
        EntryPoint(
            name=CREDENTIAL_TYPE,
            value="tamtree_shortvideo:GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
            group=GROUP_CREDENTIAL_TYPES,
        ),
        EntryPoint(
            name=MINIMAX_CREDENTIAL_TYPE,
            value="tamtree_shortvideo:MINIMAX_API_CREDENTIAL",
            group=GROUP_CREDENTIAL_TYPES,
        ),
        EntryPoint(
            name=OPENROUTER_CREDENTIAL_TYPE,
            value="tamtree_shortvideo:OPENROUTER_API_CREDENTIAL",
            group=GROUP_CREDENTIAL_TYPES,
        ),
    ]


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
        assert schema["properties"], node.manifest.name


#: Nodes that legitimately need no credential, and why — an allowlist rather
#: than a relaxed rule, so the next node that forgets its slot still fails.
#: `shortvideo.compose` renders locally through the curated runtime: it opens
#: no socket, reaches no vendor, and has nothing to authenticate to.
#: `shot_list` and `assemble` are pure data steps — the second reads the
#: workspace's own asset library, which is tenant-scoped by the engine.
NODES_WITHOUT_CREDENTIALS = {
    "shortvideo.compose",
    "shortvideo.shot_list",
    "shortvideo.assemble",
    "shortvideo.reuse",
}


def test_every_node_declares_the_credentials_it_uses() -> None:
    """V0.5's self test needed none because it opened no socket. Every node
    that reaches a paid API must show the slot in the palette, so the engine
    refuses to dispatch a step with no binding."""
    for node in NODES:
        if node.manifest.name in NODES_WITHOUT_CREDENTIALS:
            assert not node.manifest.credentials, (
                f"{node.manifest.name} is listed as needing no credential but declares one"
            )
            continue
        assert node.manifest.credentials, node.manifest.name
        for requirement in node.manifest.credentials:
            assert requirement.type in EXPECTED_CREDENTIAL_TYPES


def test_ships_a_square_icon() -> None:
    svg = (files("tamtree_shortvideo") / ICON).read_bytes()
    assert len(svg) < 20_000
    assert b"<svg" in svg
    assert b'viewBox="0 0 24 24"' in svg


def test_plugin_discovers_its_credential_types() -> None:
    """The `pyproject.toml` group is authoritative and the manifest's
    `entry_points` table cannot even express two contributions in one group
    (`dict[str, str]`), so this is what keeps the declaration honest."""
    registry = PluginRegistry()
    registry.discover(_entry_points)

    assert EXPECTED_CREDENTIAL_TYPES <= set(registry.credential_types())


def test_plugin_discovers_its_nodes() -> None:
    registry = PluginRegistry()
    registry.discover(_entry_points)

    assert EXPECTED_NODES <= set(registry.nodes())
    assert registry.node_plugins()[NODE_NAME] == PLUGIN_NAME

    plugin = registry.plugins()[PLUGIN_NAME]
    assert "node" in plugin.manifest.kinds
    # A literal, so raising the floor is a deliberate edit rather than a
    # number that drifts up with whatever SDK happens to be installed. V2.3
    # raised it from ^1.33 for `get_bounded`, which does not exist below 1.34;
    # V3.2 raised it again to ^1.36 for the curated-backend contract (1.35.0)
    # and `MediaLimits.limit_address_space` (1.36.0), neither of which exists
    # below it.
    assert plugin.manifest.contracts.sdk == "^1.36"
    # And the floor has to be one the SDK in this tree actually clears —
    # pinning above what is installed would pass every test here and refuse
    # at boot, which is the one place the mismatch is expensive.
    major, minor = (int(part) for part in CONTRACTS_VERSION.split(".")[:2])
    assert (major, minor) >= (1, 34)


def test_claims_no_permission_it_does_not_use() -> None:
    """V1.1 is the first code here that reads a secret and opens a socket — the
    token mint — so `secrets` and `network` arrive with it and not before. The
    test stays as the thing that stops the declaration drifting open: `database`
    and `filesystem` are still claims this plugin cannot make."""
    registry = PluginRegistry()
    registry.discover(_entry_points)

    capabilities = registry.plugins()[PLUGIN_NAME].manifest.capabilities
    assert set(capabilities.permissions) == {"network", "secrets"}


def test_the_declared_egress_matches_where_the_code_actually_talks() -> None:
    """Boot-inventory truth, not a runtime jail (SEC-G1): nothing gates on this
    list at run time, so its only value is being accurate for whoever reviews
    the install."""
    registry = PluginRegistry()
    registry.discover(_entry_points)

    allowlist = registry.plugins()[PLUGIN_NAME].manifest.capabilities.egress_allowlist
    assert DEFAULT_TOKEN_URI.split("/")[2] in allowlist
    assert SYNTHESIZE_URL.split("/")[2] in allowlist
    assert API_HOST.split("/")[2] in allowlist
    assert OPENROUTER_API_HOST.split("/")[2] in allowlist


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
