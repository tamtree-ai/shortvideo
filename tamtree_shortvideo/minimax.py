"""What both MiniMax nodes share: the host, the headers, and the error shape.

`minimax_submit` and `minimax_collect` are separate nodes for D3's reason —
`ExecutionContext` has no mid-node checkpoint, so a single create→poll→fetch
node cannot persist a `task_id` until the whole activity succeeds, and a worker
restart would submit twice. They are separate *steps*; they are not separate
integrations, so the vocabulary lives here.

**The retry split is stricter here than anywhere else in this plugin, and
deliberately so.** MiniMax's create API documents no client idempotency key
(§5.3), so a retried create is a second charge with no way to recognise the
first. That makes the usual question — "could this succeed on attempt two?" —
the wrong one. The right question is "do we *know* the provider did not accept
this?", and only a few answers qualify:

- **429**: rejected before processing. Nothing was created. Retryable.
- **A clean 4xx**: the request was understood and refused. Nothing was created,
  and nothing about it will change on a retry. Non-retryable, named.
- **5xx, a timeout, a dropped connection**: *unknown*. The task may exist and
  may already be billing. An automatic retry here silently doubles spend, so
  it is refused with a named error carrying the provider's `request_id` — a
  person decides whether to resubmit, which is the whole of what §5.3 means by
  "never claim exactly-once provider spend".

Collection has no such constraint: its input already contains a known
`task_id`, so polling retries freely. That asymmetry is the point of the split.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from tamtree_plugin_sdk import ExecutionContext, NodeConfigurationError

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE, MINIMAX_TOKEN_FIELD

__all__ = [
    "API_HOST",
    "CREATE_URL",
    "MAX_PROMPT_CHARACTERS",
    "MODELS",
    "RATIOS",
    "MinimaxError",
    "MinimaxSubmitAmbiguous",
    "MinimaxUnavailable",
    "ModelLimits",
    "auth_headers",
    "query_url",
    "raise_for_response",
    "request_id",
]

#: One region, declared in the manifest's egress allowlist so the boot
#: inventory an operator reviews is accurate (SEC-G1). A deployment on another
#: MiniMax region needs a change here *and* there — which is the point: the
#: declaration is only worth anything while it is true.
API_HOST: Final = "https://api.minimax.io"

CREATE_URL: Final = f"{API_HOST}/v2/video_generation"

#: Prompt ceiling from the create contract's `content[].text`.
MAX_PROMPT_CHARACTERS: Final = 7_000

#: Every ratio the create contract accepts. `9:16` is the one this plugin is
#: for; the rest are offered because refusing a supported value would be this
#: node inventing a limit MiniMax does not have.
RATIOS: Final = ("9:16", "16:9", "1:1", "3:4", "4:3", "21:9", "adaptive")


class ModelLimits:
    """One model's accepted duration range and resolutions.

    Held as data rather than validated against a remote call: the matrix is
    small, it differs per model, and checking it locally is the difference
    between a named refusal and a billed rejection.
    """

    def __init__(self, *, min_seconds: int, max_seconds: int, resolutions: tuple[str, ...]) -> None:
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.resolutions = resolutions


#: Checked against the live create reference on 2026-09-19. §7's rule applies:
#: re-check when the pin moves — MiniMax has changed this matrix before, which
#: is why the plan's original fixed 6s/10s assumption had to be corrected.
MODELS: Final = {
    "MiniMax-H3": ModelLimits(min_seconds=4, max_seconds=15, resolutions=("768P", "2K")),
    "MiniMax-H3-Max": ModelLimits(min_seconds=5, max_seconds=15, resolutions=("480P", "768P")),
}


class MinimaxError(NodeConfigurationError):
    """MiniMax understood the request and refused it.

    Non-retryable: the request was rejected rather than accepted, so nothing
    was created and nothing about a second identical attempt would differ.
    """


class MinimaxSubmitAmbiguous(NodeConfigurationError):
    """A create call whose outcome is unknown — and must not be auto-retried.

    Non-retryable on purpose, and this is the one place in the plugin where
    that class is used to *prevent* a retry rather than because a retry would
    be pointless. The task may exist and may already be billing; resubmitting
    automatically would charge twice for a clip nobody asked for twice.
    """


class MinimaxUnavailable(RuntimeError):
    """Rejected before processing — a rate limit. Safe to retry.

    Deliberately narrow. Everything else that fails without a clean answer is
    `MinimaxSubmitAmbiguous` for a create, because the retry budget is not a
    safe thing to spend on an operation that bills.
    """


async def auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    """`Authorization: Bearer …` from the bound `minimax_api` credential.

    Built here rather than through `credential_auth_headers` because that
    function lives in the server's request path; the field it reads is the
    same one, and `credentials.py` explains why the field is called `token`.
    """
    payload = await ctx.credential(MINIMAX_CREDENTIAL_TYPE)
    token = (payload.get(MINIMAX_TOKEN_FIELD) or "").strip()
    if not token:
        raise NodeConfigurationError(
            f"The {MINIMAX_CREDENTIAL_TYPE!r} credential has no API key. Paste the key from "
            "the MiniMax platform (Account Management → API Keys) into the credential."
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def query_url(task_id: str) -> str:
    return f"{API_HOST}/v2/query/video_generation/{task_id}"


def request_id(payload: Any, response: httpx.Response | None = None) -> str:
    """MiniMax's trace id for this call, or an empty string.

    §5.3 binds every error this integration raises to carry it: it is the only
    handle a duplicate charge can be traced back to the submission that caused
    it. Read from the body first, where the create contract puts it, then from
    the response headers, which some gateways set instead.
    """
    if isinstance(payload, dict):
        for key in ("request_id", "trace_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if response is not None:
        for header in ("x-request-id", "trace-id", "x-trace-id"):
            header_value = str(response.headers.get(header, "")).strip()
            if header_value:
                return header_value
    return ""


def _error_detail(payload: Any) -> str:
    """MiniMax's own words about the refusal.

    The V2 error body is `{"type": "error", "error": {"type", "message",
    "http_code"}, "request_id"}`. Neither half carries the API key.
    """
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        parts = [str(error.get(key) or "").strip() for key in ("type", "message")]
        return " — ".join(part for part in parts if part)
    if isinstance(error, str):
        return error.strip()
    return ""


def body_of(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def raise_for_response(response: httpx.Response, *, action: str, ambiguous: bool) -> Any:
    """Turn a non-2xx answer into the right kind of error, or return the body.

    `ambiguous` says whether an unknown outcome could have cost money — true
    for a create, false for a poll. It is the only thing that decides whether a
    5xx keeps its retry budget or becomes a named refusal.
    """
    payload = body_of(response)
    trace = request_id(payload, response)
    suffix = f" (MiniMax request id {trace})" if trace else ""

    if response.status_code == 429:
        raise MinimaxUnavailable(
            f"MiniMax rate-limited the {action} and did not process it{suffix} — "
            "this is worth another attempt."
        )
    if response.status_code >= 500:
        if ambiguous:
            raise MinimaxSubmitAmbiguous(
                f"MiniMax answered {response.status_code} to the {action}{suffix}, so it is "
                "unknown whether the clip was accepted. It is not retried automatically: a "
                "second create would be a second charge, and MiniMax documents no idempotency "
                "key that could recognise the first. Check the task list for a clip matching "
                "this prompt before running this step again."
            )
        raise MinimaxUnavailable(
            f"MiniMax answered {response.status_code} to the {action}{suffix} — "
            "this is worth another attempt."
        )
    if response.status_code >= 400:
        detail = _error_detail(payload) or f"HTTP {response.status_code}"
        raise MinimaxError(f"MiniMax refused the {action}: {detail}{suffix}.")

    if payload is None:
        raise MinimaxError(
            f"MiniMax answered the {action} with something that is not JSON{suffix}."
        )
    return payload
