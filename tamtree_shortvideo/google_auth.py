"""Mint Google Cloud access tokens from a service-account key, in the node.

This is the half of §5.1 that is *code*. `credentials.py` declares the shape the
user fills in; this module turns that shape into an `Authorization` header.

**Why the plugin mints its own token.** `ctx.credential(type_)` hands back the
decrypted payload and nothing else — core has no per-type token minter, and
`tamtree.credential_providers` is the instance-wide *storage* backend, not that
seam (§5.1). So the exchange lives here, and is reviewed as plugin code: key
material is write-only, never logged, never interpolated into an error; the
signed assertion and the minted token never leave this module except as a
header value.

**Why the errors are split in two.** The engine retries a node that raises, and
only `NodeConfigurationError` marks a step non-retryable with its sentence
carried verbatim to the run banner
(`packages/engine/tamtree_engine/activities/pipeline.py:1490-1510 @ 90e82780`).
A malformed key and a rejected assertion are the author's to fix and can never
succeed on attempt two, so both subclass it. A 5xx or a dropped connection is
exactly what a retry is for, so `TokenEndpointUnavailable` deliberately does
not. This split is the *only* place a bad or expired key is ever reported:
*Test connection* cannot probe this credential (see `credentials.py`), so a
loud, named error here is what liveness proof means for this type.

**Why the cache is process-local and unlocked.** There is no cross-worker token
store to write into, so the cache lives for as long as the worker process. It
carries no lock on purpose: an `asyncio.Lock` created at import binds to
whichever event loop first touches it, which is a real hazard in a worker that
runs activities on more than one loop, and the failure it would prevent — two
coroutines minting concurrently on a cold cache — costs one redundant, free
request and leaves both callers with a valid token. Correctness does not depend
on the cache, only latency does.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import jwt
from tamtree_plugin_sdk import ExecutionContext, NodeConfigurationError

from tamtree_shortvideo.credentials import CREDENTIAL_TYPE, KEY_FIELD

__all__ = [
    "CLOUD_PLATFORM_SCOPE",
    "DEFAULT_TOKEN_URI",
    "ServiceAccountKey",
    "ServiceAccountKeyError",
    "TokenEndpointUnavailable",
    "TokenMintError",
    "access_token",
    "clear_token_cache",
]

#: The only scope `texttospeech.text.synthesize` documents. There is no narrower
#: one, so least privilege for this credential is the service account's IAM role
#: and a dedicated project — not this string (§5.1).
CLOUD_PLATFORM_SCOPE: Final = "https://www.googleapis.com/auth/cloud-platform"

#: Where the JWT is exchanged. A key file carries its own `token_uri`; this is
#: the value every current one carries, and the fallback when it is absent.
DEFAULT_TOKEN_URI: Final = "https://oauth2.googleapis.com/token"

_JWT_GRANT_TYPE: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Google rejects an assertion whose `iat` is in the future, which is what a
#: worker clock running fast produces. Backdating by five minutes absorbs the
#: skew that is actually survivable; a clock running *slow* past this margin
#: mints an already-expired assertion, and nothing a client does can fix that —
#: it surfaces as a named `TokenMintError` telling the operator to check the
#: clock, rather than as a mystery 400.
_CLOCK_SKEW_SECONDS: Final = 300

#: Assertion lifetime measured from the backdated `iat`. Google caps `exp - iat`
#: at one hour and refuses anything longer.
_ASSERTION_LIFETIME_SECONDS: Final = 3600

#: Re-mint this far before the token actually lapses, so a token fetched at the
#: edge of its life does not expire mid-request.
_EXPIRY_MARGIN_SECONDS: Final = 60

#: Ceiling on how much of Google's error body is quoted back. The body carries
#: `error`/`error_description` and never key material, but an error message is
#: not the place to paste an unbounded remote string.
_DETAIL_LIMIT: Final = 300


class ServiceAccountKeyError(NodeConfigurationError):
    """The stored credential is not a usable service-account key file.

    Non-retryable: re-running cannot turn a malformed key into a valid one. The
    message names the defect and never the key material.
    """


class TokenMintError(NodeConfigurationError):
    """Google refused the signed assertion — a bad, revoked or expired key.

    Non-retryable for the same reason, and the one place this credential's
    liveness is ever proven.
    """


class TokenEndpointUnavailable(RuntimeError):
    """The token endpoint did not answer, or answered 5xx.

    Deliberately *not* a `NodeConfigurationError`: nothing is wrong with the
    credential, so the engine's ordinary retry budget should have its go.
    """


@dataclass(frozen=True)
class ServiceAccountKey:
    """The four fields of a key file this module needs, and nothing else.

    `private_key` is `repr=False` and `compare=False` so the PEM cannot reach a
    log line, a traceback frame summary or a pytest assertion diff through the
    dataclass's own machinery. `fingerprint` stands in for it wherever the key
    needs to be *identified* rather than used.
    """

    client_email: str
    private_key: str = field(repr=False, compare=False)
    token_uri: str = DEFAULT_TOKEN_URI
    project_id: str = ""

    @property
    def fingerprint(self) -> str:
        """A one-way handle for the key material, used as a cache discriminator.

        Hashing the PEM rather than trusting `private_key_id` means a rotated
        key never serves a stale token even if the file omitted that field.
        """
        return hashlib.sha256(self.private_key.encode("utf-8")).hexdigest()

    @classmethod
    def parse(cls, payload: dict[str, str]) -> ServiceAccountKey:
        """Read the stored credential payload, or say precisely what is wrong.

        Every failure here is the author's to fix, and every message is written
        for them: which field is missing, what the file should be. None of them
        quotes a value.
        """
        blob = (payload.get(KEY_FIELD) or "").strip()
        if not blob:
            raise ServiceAccountKeyError(
                f"The {CREDENTIAL_TYPE!r} credential has no {KEY_FIELD!r} — paste the whole "
                "JSON key file downloaded from the Google Cloud console into that field."
            )
        try:
            parsed: Any = json.loads(blob)
        except json.JSONDecodeError as error:
            raise ServiceAccountKeyError(
                f"The {CREDENTIAL_TYPE!r} credential is not valid JSON "
                f"(line {error.lineno}, column {error.colno}) — paste the key file exactly as "
                "downloaded, without reformatting it or removing the surrounding braces."
            ) from None
        if not isinstance(parsed, dict):
            raise ServiceAccountKeyError(
                f"The {CREDENTIAL_TYPE!r} credential is valid JSON but not an object — it should "
                "be the whole key file, which starts with `{` and contains `client_email`."
            )

        key_type = parsed.get("type")
        if key_type and key_type != "service_account":
            raise ServiceAccountKeyError(
                f"The {CREDENTIAL_TYPE!r} credential is a {key_type!r} key file, not a "
                "service account one. Create a key on a *service account* under IAM & Admin "
                "→ Service Accounts → Keys."
            )
        missing = [name for name in ("client_email", "private_key") if not parsed.get(name)]
        if missing:
            raise ServiceAccountKeyError(
                f"The {CREDENTIAL_TYPE!r} credential is missing {', '.join(missing)} — that is "
                "not a complete service-account key file. Download a fresh JSON key and paste "
                "all of it."
            )
        return cls(
            client_email=str(parsed["client_email"]),
            private_key=str(parsed["private_key"]),
            token_uri=str(parsed.get("token_uri") or DEFAULT_TOKEN_URI),
            project_id=str(parsed.get("project_id") or ""),
        )


@dataclass(frozen=True)
class _CachedToken:
    token: str = field(repr=False, compare=False)
    #: `time.monotonic()`, not wall time — a clock correction mid-run must not
    #: hand out a token the cache thinks is fresh.
    expires_at: float


#: (client_email, token_uri, scope, key fingerprint) -> token. Process-local and
#: unlocked; see the module docstring.
_TOKEN_CACHE: dict[tuple[str, str, str, str], _CachedToken] = {}


def clear_token_cache() -> None:
    """Drop every cached token. For tests, and for a worker that wants a clean
    slate; production never needs it, because expiry does this job."""
    _TOKEN_CACHE.clear()


def _assertion(key: ServiceAccountKey, *, scope: str, now: float) -> str:
    """The signed JWT Google exchanges for an access token.

    `jwt.encode` is where an unparseable PEM finally surfaces — the string
    passed validation as JSON but is not a key — so it is translated into the
    same author-facing error as every other defect in the file.
    """
    issued_at = int(now) - _CLOCK_SKEW_SECONDS
    claims = {
        "iss": key.client_email,
        "scope": scope,
        "aud": key.token_uri,
        "iat": issued_at,
        "exp": issued_at + _ASSERTION_LIFETIME_SECONDS,
    }
    try:
        return jwt.encode(claims, key.private_key, algorithm="RS256")
    except Exception as error:  # noqa: BLE001 — pyjwt raises several unrelated types here
        raise ServiceAccountKeyError(
            f"The {CREDENTIAL_TYPE!r} credential's private key could not be used to sign "
            f"({type(error).__name__}) — the `private_key` in the key file is not a usable "
            "RSA PEM. Download a fresh JSON key rather than editing this one."
        ) from None


def _detail(response: httpx.Response) -> str:
    """Google's own words about the refusal, bounded.

    The token endpoint answers `{"error": …, "error_description": …}`; neither
    carries key material, and neither does the fallback text.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("error") or "").strip()
        description = str(body.get("error_description") or "").strip()
        detail = " — ".join(part for part in (code, description) if part)
    else:
        detail = response.text.strip()
    return detail[:_DETAIL_LIMIT] or f"HTTP {response.status_code}"


async def access_token(
    ctx: ExecutionContext,
    *,
    scope: str = CLOUD_PLATFORM_SCOPE,
    credential_type: str = CREDENTIAL_TYPE,
) -> str:
    """A bearer token for `scope`, minted from the bound service-account key.

    Cached in process for the token's lifetime, so a flow synthesizing fifty
    phrases mints once. Every failure mode is named: see the three error classes
    above.
    """
    key = ServiceAccountKey.parse(await ctx.credential(credential_type))
    cache_key = (key.client_email, key.token_uri, scope, key.fingerprint)

    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None and cached.expires_at > time.monotonic():
        return cached.token
    _TOKEN_CACHE.pop(cache_key, None)

    assertion = _assertion(key, scope=scope, now=time.time())
    # `ctx.http()` and never raw httpx (§19.4): the SSRF policy lives on that
    # seam, and a plugin that reaches around it is the hole SEC-C1 closes.
    requested_at = time.monotonic()
    try:
        response = await ctx.http().post(
            key.token_uri,
            data={"grant_type": _JWT_GRANT_TYPE, "assertion": assertion},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as error:
        raise TokenEndpointUnavailable(
            f"Could not reach the Google token endpoint ({type(error).__name__}) — "
            "the credential may be fine; this is worth another attempt."
        ) from error

    if response.status_code >= 500:
        raise TokenEndpointUnavailable(
            f"The Google token endpoint answered {response.status_code} — "
            "the credential may be fine; this is worth another attempt."
        )
    if response.status_code >= 400:
        raise TokenMintError(
            f"Google refused the {credential_type!r} credential for "
            f"{key.client_email}: {_detail(response)}. The key is wrong, disabled, deleted, or "
            "the worker's clock is far enough out that the signed request looks expired. "
            "Check the service account still exists and issue a fresh key."
        )

    try:
        body = response.json()
    except ValueError:
        body = None
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise TokenMintError(
            f"Google accepted the {credential_type!r} credential for {key.client_email} but "
            "returned no access token. Retrying will not change that; re-issue the key."
        )

    lifetime = _lifetime_seconds(body)
    _TOKEN_CACHE[cache_key] = _CachedToken(
        token=str(token),
        expires_at=requested_at + lifetime,
    )
    # The token itself is never logged — only that one was obtained, for whom,
    # and for how long, which is what an operator reading a worker log needs.
    ctx.logger.debug("minted a Google access token for %s (%ss)", key.client_email, int(lifetime))
    return str(token)


def _lifetime_seconds(body: dict[str, Any] | None) -> float:
    """How long the cache may serve this token, margin already subtracted.

    A missing or nonsense `expires_in` is treated as "cache nothing" rather than
    guessed at: serving a token whose life we invented is worse than minting
    again.
    """
    raw = (body or {}).get("expires_in")
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, seconds - _EXPIRY_MARGIN_SECONDS)
