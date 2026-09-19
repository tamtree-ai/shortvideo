"""The in-node token mint — V1.1's whole review surface.

*Test connection* can never say "working" for a `google_service_account`
credential (`credentials.py`), so every claim this plugin makes about a key
being usable is made here. These tests are grouped by the four obligations
§5.1 names: key material stays write-only, failures are loud and named, the
assertion survives a skewed clock, and the cache neither serves a stale token
nor mints one per request.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from tamtree_plugin_sdk import NodeConfigurationError
from tamtree_plugin_sdk.testing import FakeContext, RecordingTransport

from tamtree_shortvideo import google_auth
from tamtree_shortvideo.google_auth import (
    CLOUD_PLATFORM_SCOPE,
    DEFAULT_TOKEN_URI,
    ServiceAccountKey,
    ServiceAccountKeyError,
    TokenEndpointUnavailable,
    TokenMintError,
    access_token,
)

TOKEN = "ya29.a0-a-real-looking-access-token"
CLIENT_EMAIL = "shorts@example-project.iam.gserviceaccount.com"


class _Clock:
    """A stand-in for the `time` module, so `iat`/`exp` and cache expiry are
    assertions rather than sleeps."""

    def __init__(self, wall: float = 1_760_000_000.0, mono: float = 1_000.0) -> None:
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def _ok(expires_in: Any = 3599) -> httpx.Response:
    body: dict[str, Any] = {"access_token": TOKEN, "token_type": "Bearer"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    return httpx.Response(200, json=body)


def _context(
    credential_payload: dict[str, str], responses: list[httpx.Response]
) -> tuple[FakeContext, RecordingTransport]:
    transport = RecordingTransport(responses)
    ctx = FakeContext(
        transport=transport,
        credentials={"google_service_account": credential_payload},
    )
    return ctx, transport


def _assertion_of(request: httpx.Request) -> str:
    from urllib.parse import parse_qs

    form = parse_qs(request.content.decode("utf-8"))
    return form["assertion"][0]


# -- key material stays write-only ------------------------------------------


def test_the_pem_never_reaches_a_repr(credential_payload: dict[str, str]) -> None:
    """A traceback frame summary, a pytest assertion diff and a log line that
    interpolates the object all go through `repr`. None of them may carry the
    key."""
    key = ServiceAccountKey.parse(credential_payload)

    rendered = repr(key)
    assert "BEGIN PRIVATE KEY" not in rendered
    assert key.private_key not in rendered
    assert CLIENT_EMAIL in rendered  # the identifier is not the secret


def test_the_fingerprint_is_one_way(credential_payload: dict[str, str]) -> None:
    key = ServiceAccountKey.parse(credential_payload)

    assert len(key.fingerprint) == 64
    assert key.private_key not in key.fingerprint


async def test_no_key_or_token_reaches_the_log(
    credential_payload: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    ctx, _ = _context(credential_payload, [_ok()])

    with caplog.at_level(logging.DEBUG, logger="tamtree.testing"):
        await access_token(ctx)

    logged = caplog.text
    assert TOKEN not in logged
    assert "BEGIN PRIVATE KEY" not in logged
    assert CLIENT_EMAIL in logged  # an operator still learns *who* was minted for


async def test_no_key_reaches_an_error_message(
    key_file: dict[str, Any], private_key_pem: str
) -> None:
    """The failure path is where a naive implementation pastes the payload it
    could not use. Every message here names the defect instead."""
    broken = dict(key_file)
    broken["private_key"] = (
        "-----BEGIN PRIVATE KEY-----\nnot actually a key\n-----END PRIVATE KEY-----\n"
    )
    ctx, _ = _context({"service_account_json": json.dumps(broken)}, [])

    with pytest.raises(ServiceAccountKeyError) as caught:
        await access_token(ctx)

    message = str(caught.value)
    assert broken["private_key"] not in message
    assert private_key_pem not in message
    assert "not a usable RSA PEM" in message


# -- the assertion is real, and survives a skewed clock ----------------------


async def test_the_assertion_is_a_genuine_rs256_signature(
    credential_payload: dict[str, str], private_key_pem: str
) -> None:
    """Signed for real, and verified against the matching public key — an RS256
    path exercised only against a stub could hide a key-handling bug."""
    ctx, transport = _context(credential_payload, [_ok()])

    assert await access_token(ctx) == TOKEN

    public_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    ).public_key()
    claims = jwt.decode(
        _assertion_of(transport.requests[0]),
        public_key,
        algorithms=["RS256"],
        audience=DEFAULT_TOKEN_URI,
    )
    assert claims["iss"] == CLIENT_EMAIL
    assert claims["scope"] == CLOUD_PLATFORM_SCOPE


async def test_iat_is_backdated_and_the_assertion_stays_inside_googles_hour(
    credential_payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker clock running fast is the skew that actually bites: Google
    refuses an assertion issued in the future. Backdating `iat` absorbs it, and
    `exp - iat` must still not exceed the hour Google caps at."""
    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    ctx, transport = _context(credential_payload, [_ok()])

    await access_token(ctx)

    claims = jwt.decode(_assertion_of(transport.requests[0]), options={"verify_signature": False})
    assert claims["iat"] == int(clock.wall) - 300
    assert claims["exp"] - claims["iat"] == 3600
    assert claims["iat"] < clock.wall


async def test_it_posts_a_jwt_bearer_grant_to_the_key_files_token_uri(
    credential_payload: dict[str, str],
) -> None:
    ctx, transport = _context(credential_payload, [_ok()])

    await access_token(ctx)

    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == DEFAULT_TOKEN_URI
    from urllib.parse import parse_qs

    form = parse_qs(request.content.decode("utf-8"))
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]


# -- the cache ---------------------------------------------------------------


async def test_a_live_token_is_reused(
    credential_payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mocked response for two calls: a second request would fail the
    transport outright, which is the assertion."""
    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    ctx, transport = _context(credential_payload, [_ok()])

    assert await access_token(ctx) == TOKEN
    clock.mono += 3000  # still inside 3599 - 60
    assert await access_token(ctx) == TOKEN

    assert len(transport.requests) == 1


async def test_an_expired_token_is_re_minted(
    credential_payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    second = httpx.Response(200, json={"access_token": "ya29.second", "expires_in": 3599})
    ctx, transport = _context(credential_payload, [_ok(), second])

    assert await access_token(ctx) == TOKEN
    clock.mono += 3599  # past the token's life, margin included
    assert await access_token(ctx) == "ya29.second"

    assert len(transport.requests) == 2


async def test_the_margin_re_mints_before_the_token_actually_lapses(
    credential_payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token handed out one second before expiry would lapse mid-request."""
    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    second = httpx.Response(200, json={"access_token": "ya29.second", "expires_in": 3599})
    ctx, transport = _context(credential_payload, [_ok(), second])

    await access_token(ctx)
    clock.mono += 3550  # token nominally lives to 3599; the 60s margin has bitten
    assert await access_token(ctx) == "ya29.second"
    assert len(transport.requests) == 2


async def test_a_response_without_expires_in_is_not_cached(
    credential_payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving a token whose lifetime we invented is worse than minting again."""
    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    ctx, transport = _context(credential_payload, [_ok(expires_in=None), _ok()])

    await access_token(ctx)
    await access_token(ctx)

    assert len(transport.requests) == 2


async def test_a_rotated_key_does_not_serve_the_old_token(
    key_file: dict[str, Any], private_key_pem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same service account, new key — the cache is keyed on the key material's
    fingerprint, so the rotation is honoured rather than shadowed for an hour."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    clock = _Clock()
    monkeypatch.setattr(google_auth, "time", clock)
    rotated = dict(key_file)
    rotated["private_key"] = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode("utf-8")
    )

    first, _ = _context({"service_account_json": json.dumps(key_file)}, [_ok()])
    assert await access_token(first) == TOKEN

    second_response = httpx.Response(200, json={"access_token": "ya29.rotated", "expires_in": 3599})
    second, transport = _context({"service_account_json": json.dumps(rotated)}, [second_response])
    assert await access_token(second) == "ya29.rotated"
    assert len(transport.requests) == 1


# -- loud, named failures ----------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "has no 'service_account_json'"),
        ({"service_account_json": "   "}, "has no 'service_account_json'"),
        ({"service_account_json": "not json at all"}, "is not valid JSON"),
        ({"service_account_json": '["a", "list"]'}, "not an object"),
        ({"service_account_json": '{"type": "authorized_user"}'}, "not a service account one"),
        ({"service_account_json": '{"type": "service_account"}'}, "missing client_email"),
        (
            {"service_account_json": '{"type": "service_account", "client_email": "a@b.c"}'},
            "missing private_key",
        ),
    ],
)
async def test_a_malformed_key_fails_by_name_before_any_request(
    payload: dict[str, str], expected: str
) -> None:
    """Named and non-retryable: `ServiceAccountKeyError` is a
    `NodeConfigurationError`, which the engine surfaces verbatim to the author
    and never retries. An empty response list proves nothing was sent."""
    ctx, transport = _context(payload, [])

    with pytest.raises(ServiceAccountKeyError, match=expected):
        await access_token(ctx)

    assert transport.requests == []


async def test_a_rejected_key_is_the_authors_problem_not_a_retry(
    credential_payload: dict[str, str],
) -> None:
    """The only place a bad, revoked or expired key is ever found. Google's own
    words are quoted so the author can tell `invalid_grant` from a deleted
    account, and the error is non-retryable so the message is not buried under
    `RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED`."""
    refusal = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
    )
    ctx, _ = _context(credential_payload, [refusal])

    with pytest.raises(TokenMintError) as caught:
        await access_token(ctx)

    message = str(caught.value)
    assert "invalid_grant" in message
    assert "Invalid JWT Signature." in message
    assert CLIENT_EMAIL in message
    assert "clock" in message  # the skew case an operator would otherwise chase blind
    assert isinstance(caught.value, NodeConfigurationError)


async def test_a_refusal_body_is_bounded(credential_payload: dict[str, str]) -> None:
    """An error message is not the place to paste an unbounded remote string."""
    ctx, _ = _context(credential_payload, [httpx.Response(401, text="x" * 5_000)])

    with pytest.raises(TokenMintError) as caught:
        await access_token(ctx)

    assert len(str(caught.value)) < 800


async def test_a_5xx_stays_retryable(credential_payload: dict[str, str]) -> None:
    """Nothing is wrong with the credential, so the engine's retry budget should
    have its go — which means *not* a `NodeConfigurationError`."""
    ctx, _ = _context(credential_payload, [httpx.Response(503, text="backend unavailable")])

    with pytest.raises(TokenEndpointUnavailable) as caught:
        await access_token(ctx)

    assert not isinstance(caught.value, NodeConfigurationError)


async def test_a_dropped_connection_stays_retryable(
    credential_payload: dict[str, str],
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    ctx = FakeContext(
        transport=_Exploding(explode),
        credentials={"google_service_account": credential_payload},
    )

    with pytest.raises(TokenEndpointUnavailable) as caught:
        await access_token(ctx)

    assert not isinstance(caught.value, NodeConfigurationError)


async def test_a_200_with_no_token_is_named_rather_than_silent(
    credential_payload: dict[str, str],
) -> None:
    ctx, _ = _context(credential_payload, [httpx.Response(200, json={"token_type": "Bearer"})])

    with pytest.raises(TokenMintError, match="returned no access token"):
        await access_token(ctx)


class _Exploding(RecordingTransport):
    """A transport whose handler raises rather than answering."""

    def __init__(self, handler: Any) -> None:
        super().__init__([])
        self._handler = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)  # type: ignore[no-any-return]
