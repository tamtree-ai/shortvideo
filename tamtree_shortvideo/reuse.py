"""`shortvideo.reuse` — find an artifact this workspace already paid for (V4.4).

The recovery half of the template. A short costs one narration and one video
generation per beat, and the ways a run ends early are ordinary: a beat the
provider refused, a reviewer who rejected the draft, a look someone wants to
change after approving. Every one of them should be answered by **running the
flow again with the same shot list** — and a replay must not buy again what it
already has.

So every paid artifact is saved under a name derived from *exactly what
produced it* — the model, the prompt, the duration, the voice — and this node
looks that name up before the paid step runs:

- `found` — the saved artifact, attached, with the fields it was saved with.
  The paid step on the other branch never runs.
- `missing` — the item, stamped with `asset_name`, the name to save the
  artifact under once it has been generated.

That one rule gives V4.4 all three of its recoveries without a replay engine:
re-running a failed beat submits only that beat (the others are `found`);
re-running after a rejection with one beat's prompt edited regenerates that
beat and nothing else; and re-running to change only the look (captions,
crossfade) finds every clip and the narration, so only the local render runs.

**The key is a digest of the inputs, not of the output**, because the output
is what is being avoided. Anything that would change the artifact must be in
the key — a key missing the resolution would hand back a 768P clip to a flow
asking for 1080P — so the key fields are named in the flow, next to the paid
step they describe, and a flow test holds the two to each other.

**Only artifacts this workspace saved are found.** The lookup is
`ctx.assets.resolve`, tenant-scoped by the engine; a name is a pointer into
the caller's own library, never a shared cache.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from tamtree_plugin_sdk import (
    BinaryRef,
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
)

__all__ = ["FIELDS_KEY", "NODE_NAME", "ReuseNode", "reuse_name"]

NODE_NAME: Final = "shortvideo.reuse"

#: Where the fields a reused artifact needs are kept on its asset's metadata —
#: `save_asset`'s `metadata.fields` in the flow. A nested key so the library's
#: own `tags` never collide with them.
FIELDS_KEY: Final = "fields"

_PREFIX_ALPHABET: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def reuse_name(prefix: str, key: Any) -> str:
    """`<prefix>-<24 hex>`: a digest of the canonical JSON of `key`.

    Canonical — sorted keys, no whitespace, floats as JSON writes them — so the
    same inputs name the same artifact in every process. 96 bits is far past
    collision range for one workspace's library and short enough to read in
    the Asset Library list.
    """
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class ReuseNode(ProgrammaticNode):
    """Route each item to `found` (a saved artifact exists) or `missing`."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — reuse a saved artifact",
            "description": (
                "Before a paid step, look for an artifact this workspace already generated "
                "from exactly the same inputs. Found: it is attached and the paid step is "
                "skipped. Missing: the item carries the name to save the new one under."
            ),
            "category": "Files & media",
            "icon": "icons/shortvideo.svg",
            "kind": "action",
            "inputs": [{"name": "main"}],
            "outputs": [
                {
                    "name": "found",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "asset_id": {"type": "string"},
                            "asset_name": {"type": "string"},
                            "reused": {"type": "boolean"},
                        },
                        "required": ["asset_id", "asset_name", "reused"],
                    },
                },
                {
                    "name": "missing",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "asset_name": {"type": "string"},
                            "reused": {"type": "boolean"},
                        },
                        "required": ["asset_name", "reused"],
                    },
                },
            ],
            "params": [
                {
                    "name": "prefix",
                    "label": "Kind of artifact",
                    "type": "string",
                    "default": "clip",
                    "description": (
                        "A short lowercase word the saved name starts with — clip, narration."
                    ),
                    # Part of every saved name: an expression here would let one
                    # run's data decide which library entries count as a match.
                    "literal_only": True,
                },
                {
                    "name": "key",
                    "label": "What produced it",
                    "type": "json",
                    "default": {},
                    "required": True,
                    "description": (
                        "Every input that changes the artifact — model, prompt, duration, "
                        "resolution, voice. The same values find the same artifact; leaving "
                        "one out would hand back an artifact made differently."
                    ),
                },
                {
                    "name": "attachment",
                    "label": "Attach as",
                    "type": "string",
                    "default": "data",
                    "description": "The attachment name a found artifact is put under.",
                },
            ],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        found: list[Item] = []
        missing: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            prefix = str(ctx.param("prefix", item=item) or "").strip().lower()
            if not prefix or not set(prefix) <= _PREFIX_ALPHABET:
                raise NodeConfigurationError(
                    f"Reuse: the kind {prefix!r} must be a short lowercase word, like clip."
                )
            key = ctx.param("key", item=item)
            if isinstance(key, str):
                try:
                    key = json.loads(key)
                except json.JSONDecodeError:
                    pass
            if not key:
                raise NodeConfigurationError(
                    "Reuse has no key, so every artifact would look the same. List the "
                    "inputs that produce it — model, prompt, duration."
                )
            name = reuse_name(prefix, key)
            data = dict(item.json_ or {})
            info = await ctx.assets.resolve(name=name)
            if info is None:
                missing.append(
                    Item.model_validate(
                        {
                            "json": {**data, "asset_name": name, "reused": False},
                            "binary": item.binary,
                        }
                    )
                )
                continue
            saved = info.metadata.get(FIELDS_KEY) if isinstance(info.metadata, Mapping) else None
            fields = dict(saved) if isinstance(saved, Mapping) else {}
            attachment = str(ctx.param("attachment", item=item) or "data").strip() or "data"
            ref = BinaryRef(
                id=info.id,
                file_name=info.name,
                mime_type=info.mime_type,
                size_bytes=info.size_bytes,
                storage_key=info.content_storage_key,
            )
            found.append(
                Item.model_validate(
                    {
                        # The item's own fields win over the saved ones: a beat
                        # number is the beat's, not whichever run first made
                        # the clip.
                        "json": {
                            **fields,
                            **data,
                            "asset_id": info.id,
                            "asset_name": name,
                            "reused": True,
                        },
                        "binary": {**(item.binary or {}), attachment: ref},
                    }
                )
            )
        return {"found": found, "missing": missing}
