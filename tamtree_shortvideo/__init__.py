"""Tamtree short-video plugin — narration, footage and composition for vertical shorts."""

from tamtree_shortvideo.compose import ComposeNode
from tamtree_shortvideo.credentials import (
    CREDENTIAL_TYPE,
    GOOGLE_SERVICE_ACCOUNT_CREDENTIAL,
    MINIMAX_API_CREDENTIAL,
    MINIMAX_CREDENTIAL_TYPE,
    OPENROUTER_API_CREDENTIAL,
    OPENROUTER_CREDENTIAL_TYPE,
)
from tamtree_shortvideo.google_tts import GoogleTtsNode
from tamtree_shortvideo.loudness import BACKEND as AUDIO_BACKEND
from tamtree_shortvideo.minimax_cancel import MinimaxCancelNode
from tamtree_shortvideo.minimax_collect import MinimaxCollectNode
from tamtree_shortvideo.minimax_submit import MinimaxSubmitNode
from tamtree_shortvideo.nodes import NODES
from tamtree_shortvideo.openrouter_tts import OpenRouterTtsNode
from tamtree_shortvideo.remotion import BACKEND as REMOTION_BACKEND

__all__ = [
    "AUDIO_BACKEND",
    "CREDENTIAL_TYPE",
    "GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
    "MINIMAX_API_CREDENTIAL",
    "MINIMAX_CREDENTIAL_TYPE",
    "NODES",
    "REMOTION_BACKEND",
    "OPENROUTER_API_CREDENTIAL",
    "OPENROUTER_CREDENTIAL_TYPE",
    "ComposeNode",
    "GoogleTtsNode",
    "MinimaxCancelNode",
    "MinimaxCollectNode",
    "MinimaxSubmitNode",
    "OpenRouterTtsNode",
]
