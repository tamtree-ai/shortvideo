"""Tamtree short-video plugin — narration, footage and composition for vertical shorts."""

from tamtree_shortvideo.credentials import (
    CREDENTIAL_TYPE,
    GOOGLE_SERVICE_ACCOUNT_CREDENTIAL,
)
from tamtree_shortvideo.google_tts import GoogleTtsNode
from tamtree_shortvideo.nodes import NODES

__all__ = [
    "CREDENTIAL_TYPE",
    "GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
    "NODES",
    "GoogleTtsNode",
]
