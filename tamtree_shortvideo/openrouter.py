"""What `shortvideo.openrouter_tts` needs from OpenRouter: auth, errors, cost.

**Why cost is looked up rather than computed from an operator-entered rate.**
`shortvideo.google_tts` and `minimax_collect` both make the operator supply a
`price_usd_per_*` param, because neither Google nor MiniMax hands back a
dollar figure — only units this plugin can turn into one given a rate nobody
but the operator has. OpenRouter is different: `total_cost` on
`GET /api/v1/generation?id=…` *is* the dollar figure, computed by OpenRouter's
own ledger from whatever the underlying provider actually charged. Asking the
operator to re-enter Gemini 3.1 Flash TTS's per-token rate here would be
strictly worse than reading the number OpenRouter already has — a second
transcription of a price that ages, sitting next to the one that doesn't.

**Why the lookup is retried, bounded, and allowed to fail open.** OpenRouter's
own docs describe the generation endpoint as covering every request after it
completes, but say nothing about how soon — a ledger write racing a read makes
a 404 immediately after the `/audio/speech` response ambiguous between "not
billed" and "not indexed yet". The audio is already synthesized and paid for
by the time this runs, so refusing the step over a slow ledger would throw away
work already bought. A bounded retry absorbs an ordinary race; when it is
still not there, the call is reported unpriced — `cost_usd` unset, exactly
`minimax_collect`'s shape for "no honest number available" — rather than
guessed from a rate that could go stale.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Final

import httpx
from tamtree_plugin_sdk import ExecutionContext, NodeConfigurationError

from tamtree_shortvideo.credentials import OPENROUTER_CREDENTIAL_TYPE, OPENROUTER_TOKEN_FIELD

__all__ = [
    "API_HOST",
    "GENERATION_URL",
    "MODEL_ID",
    "SPEECH_URL",
    "GenerationCost",
    "OpenRouterError",
    "OpenRouterUnavailable",
    "auth_headers",
    "generation_cost",
    "raise_for_response",
]

API_HOST: Final = "https://openrouter.ai"

SPEECH_URL: Final = f"{API_HOST}/api/v1/audio/speech"

GENERATION_URL: Final = f"{API_HOST}/api/v1/generation"

#: Hardcoded rather than a param: this node exists for one model, the way
#: `google_tts`'s `SYNTHESIZE_URL` is pinned to v1beta1 for one feature. A
#: second OpenRouter TTS model is a second node, not a param on this one — it
#: would bring its own voice list, output shape and pricing unit.
MODEL_ID: Final = "google/gemini-3.1-flash-tts-preview"

#: How long the bounded retry waits for the ledger to catch up, per attempt.
#: Three tries, backing off — under 4 seconds total, worth spending once per
#: phrase against audio that is already paid for.
_LOOKUP_BACKOFFS_SECONDS: Final = (0.5, 1.0, 2.0)


class OpenRouterError(NodeConfigurationError):
    """OpenRouter (or the provider behind it) refused the request.

    Non-retryable: a 4xx from `/audio/speech` is this step's own configuration
    — an unknown voice, an empty input — and a second identical attempt buys
    nothing.
    """


class OpenRouterUnavailable(RuntimeError):
    """The endpoint did not answer, or answered 5xx/429.

    Not a `NodeConfigurationError`: nothing is wrong with the request, so the
    engine's retry budget should have its go.
    """


class GenerationCost:
    """What the ledger says about one `/audio/speech` call, once it is there.

    `total_cost_usd` is `None` when the bounded lookup never resolved — see the
    module docstring for why that reports as unpriced rather than a guess.
    """

    __slots__ = ("tokens_prompt", "tokens_completion", "total_cost_usd")

    def __init__(
        self,
        *,
        tokens_prompt: int,
        tokens_completion: int,
        total_cost_usd: Decimal | None,
    ) -> None:
        self.tokens_prompt = tokens_prompt
        self.tokens_completion = tokens_completion
        self.total_cost_usd = total_cost_usd


async def auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    """`Authorization: Bearer …` from the bound `openrouter_api` credential."""
    payload = await ctx.credential(OPENROUTER_CREDENTIAL_TYPE)
    token = (payload.get(OPENROUTER_TOKEN_FIELD) or "").strip()
    if not token:
        raise NodeConfigurationError(
            f"The {OPENROUTER_CREDENTIAL_TYPE!r} credential has no API key. Paste the key from "
            "openrouter.ai (Settings → Keys) into the credential."
        )
    return {"Authorization": f"Bearer {token}"}


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        detail = " — ".join(part for part in (code, message) if part)
    else:
        detail = str(error or "").strip()
    return detail[:300] or f"HTTP {response.status_code}"


def raise_for_response(response: httpx.Response, *, action: str) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise OpenRouterUnavailable(
            f"OpenRouter answered {response.status_code} to the {action} — "
            "this is worth another attempt."
        )
    if response.status_code >= 400:
        raise OpenRouterError(
            f"OpenRouter refused the {action} ({response.status_code}): {_detail(response)}. "
            "Check the voice name and the input on this step; retrying an identical request "
            "will not help."
        )


async def generation_cost(
    ctx: ExecutionContext, generation_id: str, *, headers: dict[str, str]
) -> GenerationCost | None:
    """`total_cost`/token counts for one call, or `None` if the ledger never
    caught up within the bounded retry. See the module docstring."""
    if not generation_id:
        return None
    client = ctx.http()
    for attempt, backoff in enumerate((*_LOOKUP_BACKOFFS_SECONDS, None)):
        try:
            response = await client.get(
                GENERATION_URL, params={"id": generation_id}, headers=headers
            )
        except httpx.HTTPError:
            return None
        if response.status_code == 200:
            body = _body(response)
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                return None
            cost = data.get("total_cost")
            return GenerationCost(
                tokens_prompt=_int(data.get("tokens_prompt")),
                tokens_completion=_int(data.get("tokens_completion")),
                total_cost_usd=Decimal(str(cost)) if isinstance(cost, (int, float)) else None,
            )
        if response.status_code != 404:
            return None
        if backoff is not None:
            await asyncio.sleep(backoff)
    return None


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
