"""What both OpenRouter video nodes share: the URLs, the model matrix, the errors.

**Why this route exists next to `minimax_*`.** OpenRouter resells MiniMax's H3
models from the same prepaid balance `shortvideo.openrouter_tts` draws on, so
one `openrouter_api` credential can pay for a whole short — narration and
footage — with no MiniMax or GCP account. It also answers the question the
direct route cannot: OpenRouter returns what a job actually cost
(`usage.cost`), so this route has no operator-entered rate at all. The MiniMax
nodes stay exactly as they are; this adds a way to pay, it replaces nothing.

**The retry split is `minimax.py`'s, unchanged, for the same reason.**
OpenRouter's `POST /api/v1/videos` documents no client idempotency key either,
so a retried create is a second charge nothing can recognise:

- **429**: rejected before processing. Retryable.
- **A clean 4xx** — including **402** (insufficient credits) and **403** (a
  key's spend limit) — was understood and refused. Nothing was created.
  Non-retryable, named.
- **5xx, a timeout, a dropped connection**: *unknown*. A job may exist and may
  already be billing, so a create refuses to spend its retry budget on it.

**There is no cancel.** OpenRouter documents `cancelled` and `expired` as
statuses a job can end in, and no endpoint a caller can use to put it there —
so unlike `minimax_collect`, a cancelled run here stops waiting and leaves the
job to finish (and bill) on its own. Nothing in this module claims otherwise.

**The download never leaves `openrouter.ai`.** The finished clip is streamed
through `GET /api/v1/videos/{id}/content`, authenticated with the same bearer
token as the poll — so the token only ever goes to the one host it was issued
for, and the egress inventory needs no undocumented CDN host (the gap
`minimax_collect` has to acknowledge).
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import quote

import httpx

from tamtree_shortvideo.openrouter import API_HOST, OpenRouterError, OpenRouterUnavailable

__all__ = [
    "ASPECT_RATIOS",
    "CREATE_URL",
    "MAX_PROMPT_CHARACTERS",
    "MODELS",
    "OpenRouterVideoNotReady",
    "OpenRouterVideoSubmitAmbiguous",
    "VideoModel",
    "body_of",
    "content_url",
    "error_detail",
    "job_url",
    "raise_for_video_response",
]

CREATE_URL: Final = f"{API_HOST}/api/v1/videos"

#: Not a documented OpenRouter limit — OpenRouter publishes none for `prompt`.
#: MiniMax's own create contract caps a prompt at 7,000 characters, and both
#: models here are MiniMax's, so a longer prompt would be refused upstream
#: after the request had already been routed. Checking it here is free.
MAX_PROMPT_CHARACTERS: Final = 7_000

#: Every ratio both models list in `/api/v1/videos/models` on 2026-09-23.
ASPECT_RATIOS: Final = ("9:16", "16:9", "1:1", "3:4", "4:3", "21:9")


class VideoModel:
    """One OpenRouter video model's accepted durations and resolutions.

    Held as data, like `minimax.MODELS`: checking locally is the difference
    between a named refusal and a routed 400. `generates_audio` records whether
    the model makes its own soundtrack — the submit node always turns that off,
    because the short's audio is the narration and the mix, not the model's.
    """

    def __init__(
        self,
        *,
        label: str,
        min_seconds: int,
        max_seconds: int,
        resolutions: tuple[str, ...],
        generates_audio: bool,
    ) -> None:
        self.label = label
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.resolutions = resolutions
        self.generates_audio = generates_audio


#: Read from `GET https://openrouter.ai/api/v1/videos/models` on 2026-09-23
#: (public, no key). Re-check when OpenRouter changes either row: note that H3
#: is offered at **2K only** here, where MiniMax direct also offers 768P — the
#: two surfaces do not share a matrix, which is why this is not `minimax.MODELS`.
#: Prices at that date, for the README rather than for any calculation (cost is
#: always OpenRouter's own `usage.cost`): H3-Max $0.05/s at 480p and $0.08/s at
#: 768p; H3 $0.13/s, plus $0.04 per reference image.
MODELS: Final = {
    "minimax/hailuo-3-max": VideoModel(
        label="MiniMax H3 Max — 5–15s, 480p or 768p",
        min_seconds=5,
        max_seconds=15,
        resolutions=("480p", "768p"),
        generates_audio=False,
    ),
    "minimax/hailuo-3": VideoModel(
        label="MiniMax H3 — 5–15s, 2K",
        min_seconds=5,
        max_seconds=15,
        resolutions=("2K",),
        generates_audio=True,
    ),
}


class OpenRouterVideoSubmitAmbiguous(OpenRouterError):
    """A create call whose outcome is unknown — and must not be auto-retried.

    Subclasses `OpenRouterError` so it is a `NodeConfigurationError` and the
    engine will not retry it; that is the point. A job may exist and be
    billing, and only a person looking at the OpenRouter activity log can tell.
    """


class OpenRouterVideoNotReady(OpenRouterError):
    """The wait ran out before the job finished. Nothing failed or was lost.

    Non-retryable for `minimax.MinimaxNotReady`'s reason: repeating the wait on
    the engine's schedule is not what the author asked for, and running the
    collect step again on the same job id is safe and free.
    """


def job_url(job_id: str) -> str:
    return f"{CREATE_URL}/{quote(job_id, safe='')}"


def content_url(job_id: str, index: int = 0) -> str:
    """The authenticated download for a finished job.

    Built from the job id rather than read from `unsigned_urls`, so the host a
    bearer token is sent to is fixed by this module and never by a response.
    """
    return f"{job_url(job_id)}/content?index={index}"


def body_of(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def error_detail(payload: Any, response: httpx.Response | None = None) -> str:
    """OpenRouter's own words about a refusal: `{"error": {"code", "message"}}`."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        detail = " — ".join(part for part in (code, message) if part)
    else:
        detail = str(error or "").strip()
    if not detail and response is not None:
        detail = response.text.strip()[:300] or f"HTTP {response.status_code}"
    return detail[:300]


def raise_for_video_response(response: httpx.Response, *, action: str, ambiguous: bool) -> Any:
    """Turn a non-2xx answer into the right error, or return the parsed body.

    `ambiguous` is true only for a create: it decides whether a 5xx keeps its
    retry budget or becomes a named refusal. (`openrouter.raise_for_response`
    is the TTS node's version; its advice is about voices, and a create needs
    the ambiguous branch it does not have.)
    """
    payload = body_of(response)
    status = response.status_code

    if status == 429:
        raise OpenRouterUnavailable(
            f"OpenRouter rate-limited the {action} and did not process it — "
            "this is worth another attempt."
        )
    if status >= 500:
        if ambiguous:
            raise OpenRouterVideoSubmitAmbiguous(
                f"OpenRouter answered {status} to the {action}, so it is unknown whether the "
                "job was accepted. It is not retried automatically: a second create would be a "
                "second charge, and OpenRouter documents no idempotency key that could "
                "recognise the first. Check openrouter.ai → Activity for a video job matching "
                "this prompt before running this step again."
            )
        raise OpenRouterUnavailable(
            f"OpenRouter answered {status} to the {action} — this is worth another attempt."
        )
    if status == 402:
        raise OpenRouterError(
            f"OpenRouter refused the {action}: the account has too few credits "
            f"({error_detail(payload, response)}). Nothing was generated or charged. Top up at "
            "openrouter.ai/credits and run this step again."
        )
    if status == 403:
        raise OpenRouterError(
            f"OpenRouter refused the {action} ({error_detail(payload, response)}). The usual "
            "cause is a spend limit on this API key. Nothing was generated or charged; raise the "
            "key's limit at openrouter.ai/settings/keys, or use another key."
        )
    if status >= 400:
        raise OpenRouterError(
            f"OpenRouter refused the {action} ({status}): {error_detail(payload, response)}. "
            "Retrying an identical request will not help."
        )
    if payload is None:
        raise OpenRouterError(f"OpenRouter answered the {action} with something that is not JSON.")
    return payload
