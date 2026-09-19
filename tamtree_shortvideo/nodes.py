"""The V0.5 skeleton node.

`shortvideo.selftest` exists to prove the plugin *wiring* end to end — entry
point, manifest, contracts pin, palette registration, icon resource, contract
tests, `TAMTREE_DEV_PLUGINS_DIR` discovery — before a single real node is
written against a paid provider. It computes locally, takes no credential and
opens no socket, so a green run here means the packaging is sound and nothing
else.

It is scaffolding, and Wave 1 removes it. Nothing downstream should reference
`shortvideo.selftest`; it is safe to delete only because this distribution has
not been published yet (see `01-plan.md` §5.6 — a rename or removal after
publish is a break).
"""

from __future__ import annotations

from typing import Any, ClassVar

from tamtree_plugin_sdk import (
    CONTRACTS_VERSION,
    ExecutionContext,
    Item,
    NodeManifest,
    ProgrammaticNode,
)

__all__ = ["NODES", "ShortVideoSelfTestNode"]

#: One place, so the manifest, the tests and the node body cannot drift.
NODE_NAME = "shortvideo.selftest"
ICON = "icons/shortvideo.svg"
CATEGORY = "Files & media"


class ShortVideoSelfTestNode(ProgrammaticNode):
    """Echo each input item, stamped with what the host actually loaded."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — Self test",
            "description": (
                "Verify the short-video plugin is installed and loadable. "
                "Echoes its input with the contracts version the host provides."
            ),
            "category": CATEGORY,
            "icon": ICON,
            "kind": "action",
            "inputs": [{"name": "main"}],
            "outputs": [
                {
                    "name": "main",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                            "plugin": {"type": "string"},
                            "node": {"type": "string"},
                            "contracts_version": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["ok", "plugin", "node", "contracts_version"],
                    },
                }
            ],
            "params": [
                {
                    "name": "note",
                    "label": "Note",
                    "type": "string",
                    "default": "",
                    "description": (
                        "Optional text echoed straight back — handy for telling two "
                        "instances of this node apart in a run's output."
                    ),
                }
            ],
            # No credentials on purpose: this node must stay runnable on a fresh
            # install where nothing has been configured yet.
            "credentials": [],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        outputs: list[Item] = []
        # §6.6: one pass over the input, each param resolved per item so
        # `{{ $json.note }}` behaves the way it does on every other node. An
        # empty input still produces one row — a self test that says nothing
        # when handed nothing is not a self test.
        for item in ctx.input_items() or [Item()]:
            note = ctx.param("note", item=item)
            body: dict[str, Any] = {
                "ok": True,
                "plugin": "shortvideo",
                "node": NODE_NAME,
                "contracts_version": CONTRACTS_VERSION,
                "note": "" if note is None else str(note),
            }
            outputs.append(Item.model_validate({"json": body}))
        return {"main": outputs}


NODES = [ShortVideoSelfTestNode()]
