"""V4.6: the `Short-form video` package installs as the catalog would install it.

Two layers, because this repo's venv does not carry the server:

- **Always:** the manifest validates against the SDK's `TemplateManifest`,
  every resource exists and parses as a Flow, every credential slot a flow
  binds is declared with the type its node asks for (and every declared slot is
  used), and the package's plugin requirement names *this* plugin at a version
  it satisfies.
- **When `tamtree_server` is importable** (run from the product tree:
  `uv run pytest ~/sites/tamtree-plugins/shortvideo/tests/test_template_package.py`):
  the server's own `load_package_dir`, secret scan, external-slot lint and
  plugin-requirement check — the functions an install actually calls.

What it cannot do is run the flow: that needs a live instance and real keys.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from tamtree_sdk.flowdef import FlowDefinition
from tamtree_sdk.resources import TemplateManifest
from tamtree_sdk.semver import satisfies

from tamtree_shortvideo import NODES

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "template"
MANIFESTS = {node.manifest.name: node.manifest for node in NODES}


def _manifest() -> TemplateManifest:
    return TemplateManifest.model_validate(yaml.safe_load((PACKAGE / "template.yaml").read_text()))


def _flows() -> dict[str, FlowDefinition]:
    flows: dict[str, FlowDefinition] = {}
    for path in _manifest().spec.resources:
        document: dict[str, Any] = yaml.safe_load((PACKAGE / path).read_text())
        assert document["kind"] == "Flow", path
        flows[document["metadata"]["name"]] = FlowDefinition.model_validate(document["spec"])
    return flows


def _plugin_manifest() -> dict[str, Any]:
    return tomllib.loads((ROOT / "tamtree_shortvideo" / "tamtree-plugin.toml").read_text())


def test_the_manifest_validates_and_every_resource_exists() -> None:
    manifest = _manifest()

    assert manifest.metadata.name == "short-form-video"
    for path in manifest.spec.resources:
        assert (PACKAGE / path).is_file(), path


def test_the_loop_body_is_listed_before_the_flow_that_runs_it() -> None:
    order = [Path(p).stem for p in _manifest().spec.resources]

    assert order.index("generate-one-beat") < order.index("short-form-video")


def test_every_bound_credential_slot_is_declared_with_the_right_type() -> None:
    declared = {slot.slot: slot.type for slot in _manifest().spec.requires.credentials}
    used: set[str] = set()
    for name, flow in _flows().items():
        for node in flow.nodes:
            for credential_type, slot in node.credentials.items():
                assert declared.get(slot) == credential_type, (
                    f"{name}/{node.id} binds {credential_type} to undeclared slot {slot!r}"
                )
                used.add(slot)

    # A declared slot nothing binds would make an importer supply a key the
    # package never uses.
    assert used == set(declared)


def test_the_package_requires_this_plugin_at_a_version_it_is() -> None:
    plugin = _plugin_manifest()["plugin"]
    (requirement,) = [r for r in _manifest().spec.requires.plugins if r.name == plugin["name"]]

    assert satisfies(plugin["version"], requirement.contracts["plugin"])


def test_every_node_the_package_uses_comes_from_a_required_plugin() -> None:
    """A `shortvideo.*` step with the plugin missing from `requires` would
    install and then fail at plan time on somebody else's instance."""
    required = {r.name for r in _manifest().spec.requires.plugins}
    for flow in _flows().values():
        for node in flow.nodes:
            if node.type.startswith("shortvideo."):
                assert "shortvideo" in required
                assert node.type in MANIFESTS, node.type
            else:
                assert node.type.startswith("tamtree."), node.type


def test_the_catalog_says_self_hosted_only_and_what_a_run_can_spend() -> None:
    """V4.5: the three things an importer must know before the first run."""
    meta = _manifest().metadata
    assert meta.catalog is not None

    assert "self-hosted-only" in meta.catalog.tags
    assert "SELF-HOSTED ONLY" in meta.description
    assert "at most 8 MiniMax H3 Max video generations" in " ".join(meta.description.split())
    shot_list = next(n for n in _flows()["short-form-video"].nodes if n.id == "shot_list")
    # The ceiling the description promises is the one the shot list enforces.
    assert shot_list.params["max_beats"] == 8


# --- the server's own install-time checks -------------------------------------


def test_the_server_loads_scans_and_accepts_the_package() -> None:
    template_io = pytest.importorskip("tamtree_server.template_io")
    from tamtree_sdk import PluginRegistry

    package = template_io.load_package_dir(PACKAGE)
    template_io.scan_package_for_secrets(package)
    template_io.lint_external_slot_refs(package)

    registry = PluginRegistry()
    registry.discover()
    if "shortvideo" not in registry.plugins():
        pytest.skip("the shortvideo plugin is not installed in this environment")
    template_io.check_plugin_requirements(package, registry)
