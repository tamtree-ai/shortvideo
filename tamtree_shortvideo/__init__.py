"""Tamtree short-video plugin — narration, footage and composition for vertical shorts."""

from tamtree_shortvideo.credentials import (
    CREDENTIAL_TYPE,
    GOOGLE_SERVICE_ACCOUNT_CREDENTIAL,
    MINIMAX_API_CREDENTIAL,
    MINIMAX_CREDENTIAL_TYPE,
)
from tamtree_shortvideo.google_tts import GoogleTtsNode
from tamtree_shortvideo.minimax_cancel import MinimaxCancelNode
from tamtree_shortvideo.minimax_collect import MinimaxCollectNode
from tamtree_shortvideo.minimax_submit import MinimaxSubmitNode
from tamtree_shortvideo.nodes import NODES

__all__ = [
    "CREDENTIAL_TYPE",
    "GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
    "MINIMAX_API_CREDENTIAL",
    "MINIMAX_CREDENTIAL_TYPE",
    "NODES",
    "GoogleTtsNode",
    "MinimaxCancelNode",
    "MinimaxCollectNode",
    "MinimaxSubmitNode",
]
