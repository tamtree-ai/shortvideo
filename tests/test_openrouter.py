"""`openrouter.py` — auth, the retryable/non-retryable split, and the bounded
cost lookup `openrouter_tts` depends on to avoid guessing a price."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from tamtree_plugin_sdk import NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeContext, RecordingTransport

from tamtree_shortvideo.credentials import OPENROUTER_CREDENTIAL_TYPE
from tamtree_shortvideo.openrouter import (
    OpenRouterError,
    OpenRouterUnavailable,
    auth_headers,
    generation_cost,
    raise_for_response,
)

#: Captured before any test monkeypatches `openrouter.asyncio.sleep` — that
#: patches the real `asyncio` module (`openrouter.asyncio` *is* `asyncio`, not
#: a copy), so a replacement that itself calls `asyncio.sleep` would recurse
#: into its own patch. Calling this instead is instant and does not recurse.
_REAL_SLEEP = asyncio.sleep


def _context(
    responses: list[httpx.Response], *, token: str = "sk-or-v1-test"
) -> tuple[FakeContext, RecordingTransport]:
    transport = RecordingTransport(responses)
    ctx = FakeContext(
        transport=transport,
        inputs={},
        params={},
        credentials={OPENROUTER_CREDENTIAL_TYPE: {"token": token}},
    )
    return ctx, transport


async def test_auth_headers_reads_the_token_field() -> None:
    ctx, _ = _context([])
    assert await auth_headers(ctx) == {"Authorization": "Bearer sk-or-v1-test"}


async def test_auth_headers_refuses_an_empty_token() -> None:
    ctx, _ = _context([], token="")
    with pytest.raises(NodeConfigurationError, match="no API key"):
        await auth_headers(ctx)


def test_2xx_raises_nothing() -> None:
    raise_for_response(httpx.Response(200), action="speech synthesis")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_are_retryable(status: int) -> None:
    with pytest.raises(OpenRouterUnavailable, match="worth another attempt"):
        raise_for_response(httpx.Response(status), action="speech synthesis")


def test_a_clean_4xx_is_named_and_not_retryable() -> None:
    response = httpx.Response(
        400, json={"error": {"code": "invalid_voice", "message": "unknown voice 'Nope'"}}
    )
    with pytest.raises(OpenRouterError, match="unknown voice 'Nope'"):
        raise_for_response(response, action="speech synthesis")


async def test_generation_cost_returns_none_for_no_generation_id() -> None:
    ctx, transport = _context([])
    assert await generation_cost(ctx, "", headers={}) is None
    assert transport.requests == []


async def test_generation_cost_reads_total_cost_and_tokens_on_first_try() -> None:
    ctx, _ = _context(
        [
            httpx.Response(
                200,
                json={
                    "data": {
                        "total_cost": 0.0034,
                        "tokens_prompt": 12,
                        "tokens_completion": 340,
                    }
                },
            )
        ]
    )
    result = await generation_cost(ctx, "gen-1", headers={})
    assert result is not None
    assert result.total_cost_usd == Decimal("0.0034")
    assert result.tokens_prompt == 12
    assert result.tokens_completion == 340


async def test_generation_cost_retries_through_a_not_yet_indexed_404(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "tamtree_shortvideo.openrouter.asyncio.sleep",
        lambda seconds: slept.append(seconds) or _REAL_SLEEP(0),
    )
    ctx, _ = _context(
        [
            httpx.Response(404),
            httpx.Response(404),
            httpx.Response(200, json={"data": {"total_cost": 0.01}}),
        ]
    )
    result = await generation_cost(ctx, "gen-1", headers={})
    assert result is not None
    assert result.total_cost_usd == Decimal("0.01")
    assert len(slept) == 2


async def test_generation_cost_gives_up_after_the_bounded_retry(monkeypatch) -> None:
    monkeypatch.setattr(
        "tamtree_shortvideo.openrouter.asyncio.sleep", lambda seconds: _REAL_SLEEP(0)
    )
    ctx, _ = _context([httpx.Response(404)] * 4)
    assert await generation_cost(ctx, "gen-1", headers={}) is None


async def test_generation_cost_does_not_retry_a_non_404_failure() -> None:
    ctx, _ = _context([httpx.Response(500)])
    assert await generation_cost(ctx, "gen-1", headers={}) is None


async def test_generation_cost_leaves_cost_none_when_the_ledger_field_is_not_numeric() -> None:
    """A response that answers but carries no usable price is unpriced, not
    zero and not a parse error — the same posture `minimax_collect` takes."""
    ctx, _ = _context(
        [httpx.Response(200, json={"data": {"total_cost": None, "tokens_prompt": 5}})]
    )
    result = await generation_cost(ctx, "gen-1", headers={})
    assert result is not None
    assert result.total_cost_usd is None
    assert result.tokens_prompt == 5
