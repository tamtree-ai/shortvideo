"""The nodes this distribution contributes, and the two things they share.

One module so `NODES` has a single home: the `tamtree.nodes` entry point reads
this list, and a node that is written but never added here is a node that does
not exist as far as the palette is concerned.

`shortvideo.selftest` lived here through V0.5 to prove the packaging — entry
point, manifest, contracts pin, icon, discovery — before any paid provider was
involved. V1.2 removes it, which §5.6 permits only because this distribution
has never been published: after a release, a node id is a promise.
"""

from __future__ import annotations

from typing import Final

from tamtree_plugin_sdk import Node

from tamtree_shortvideo.google_tts import GoogleTtsNode

__all__ = ["CATEGORY", "ICON", "NODES"]

#: Where these land in the palette, and the icon they carry. One constant each,
#: so the manifests and the registration tests cannot drift apart.
ICON: Final = "icons/shortvideo.svg"
CATEGORY: Final = "Files & media"

NODES: Final[list[Node]] = [GoogleTtsNode()]
