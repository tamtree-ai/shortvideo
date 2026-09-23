"""`shortvideo.openrouter_video_collect` — wait for one OpenRouter clip and save it.

The free half of the split (D3), with `minimax_collect`'s retry posture:
polling a known job creates nothing and costs nothing, so an unreachable
OpenRouter keeps its retry budget here where the create refuses to spend one.

**The output is `minimax_collect`'s shape on purpose** — `task_id`,
`duration_seconds`, `billed_seconds`, a `video/mp4` attachment — so Save
Asset, `assemble`, `TimelineV1` and `compose` cannot tell which route produced
a clip. `task_id` holds OpenRouter's job id.

**Cost is OpenRouter's own number, never a rate.** A completed job carries
`usage.cost` in USD; when it is absent the node asks the generation ledger once
more by `generation_id` (`openrouter.generation_cost`, bounded), and when that
does not resolve either the clip is reported **unpriced** rather than guessed —
`openrouter_tts`'s fail-open rule, for its reason: the clip is already made
and paid for, and refusing the step would throw it away.

**Seconds are the ones asked for.** OpenRouter's poll reports no clip length,
and it prices these models per requested second, so `billed_seconds` is the
submitted duration — read from the item `openrouter_video_submit` layered its
output onto. It is informational: the money is `usage.cost`.

**A cancelled run stops waiting, and that is all it can do.** OpenRouter
documents no cancel endpoint, so the job finishes and bills on its own; there
is no provider-side stop to attempt, unlike `minimax_collect`.

**The download is authenticated and stays on `openrouter.ai`** —
`openrouter_video.content_url` builds it from the job id, never from a URL in
the response. If OpenRouter ever answers it with a redirect to another host,
httpx drops the `Authorization` header on a cross-origin hop, and
`SafeHttpClient` re-validates every hop — so the token cannot follow the clip.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, ClassVar, Final

import httpx
from tamtree_plugin_sdk import (
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
    ResponseTooLargeError,
    get_bounded,
)

from tamtree_shortvideo.credentials import OPENROUTER_CREDENTIAL_TYPE
from tamtree_shortvideo.openrouter import (
    OpenRouterError,
    OpenRouterUnavailable,
    auth_headers,
    generation_cost,
)
from tamtree_shortvideo.openrouter_video import (
    OpenRouterVideoNotReady,
    content_url,
    job_url,
    raise_for_video_response,
)

__all__ = ["NODE_NAME", "OpenRouterVideoCollectNode"]

NODE_NAME: Final = "shortvideo.openrouter_video_collect"

#: Statuses that end the wait. Only `completed` has a clip.
TERMINAL: Final = frozenset({"completed", "failed", "cancelled", "expired"})

#: Backoff, bounded at both ends — `minimax_collect`'s numbers. OpenRouter's own
#: guide suggests ~30s between polls; this starts faster and settles there.
_BACKOFF_FACTOR: Final = 1.5
_MAX_INTERVAL_SECONDS: Final = 30.0

_MP4_BRAND_OFFSET: Final = 4
_MIME_BY_CONTAINER: Final = {"mp4": "video/mp4", "webm": "video/webm"}


class OpenRouterVideoCollectNode(ProgrammaticNode):
    """Poll one OpenRouter video job to a terminal state and save what it produced."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — OpenRouter video collect",
            "description": (
                "Wait for an OpenRouter video generation to finish and save the clip as an "
                "attachment. Takes the job id from the submit step, polls until the job is "
                "done, downloads the clip, and reports what OpenRouter charged for it."
            ),
            "category": "Files & media",
            "icon": "icons/shortvideo.svg",
            "kind": "action",
            "inputs": [{"name": "main"}],
            "outputs": [
                {
                    "name": "main",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "generation_id": {"type": "string"},
                            "status": {"type": "string"},
                            "model": {"type": "string"},
                            "duration_seconds": {"type": "number"},
                            "video": {
                                "type": "object",
                                "properties": {
                                    "binary_property": {"type": "string"},
                                    "mime_type": {"type": "string"},
                                    "size_bytes": {"type": "integer"},
                                    "file_name": {"type": "string"},
                                },
                                "required": ["binary_property", "mime_type", "size_bytes"],
                            },
                            "usage": {"type": "object"},
                            "billed_seconds": {"type": "number"},
                            "priced": {"type": "boolean"},
                            "cost_usd": {"type": "string"},
                            "polls": {"type": "integer"},
                            "waited_seconds": {"type": "number"},
                        },
                        "required": ["task_id", "status", "video"],
                    },
                }
            ],
            "params": [
                {
                    "name": "task_id",
                    "label": "Job id",
                    "type": "string",
                    "default": "",
                    "required": True,
                    "description": (
                        "The id OpenRouter video submit returned. Map it from that step — "
                        "{{ $json.task_id }}."
                    ),
                },
                {
                    "name": "max_wait_seconds",
                    "label": "Give up waiting after (seconds)",
                    "type": "number",
                    "default": 900,
                    "description": (
                        "The whole budget for the wait, not one poll. Running out does not "
                        "cancel or lose the clip — the job keeps going and this step can be "
                        "run again on the same job id to collect it."
                    ),
                },
                {
                    "name": "poll_interval_seconds",
                    "label": "First poll interval (seconds)",
                    "type": "number",
                    "default": 5,
                    "description": (
                        "The gap before the first re-check. It grows by half each time, up "
                        "to 30s — a tight poll buys nothing on a job that takes minutes."
                    ),
                },
                {
                    "name": "max_download_megabytes",
                    "label": "Refuse a clip larger than (MB)",
                    "type": "number",
                    "default": 256,
                    "description": (
                        "A real cap: an over-size clip is abandoned mid-download rather "
                        "than buffered and then refused."
                    ),
                },
                {
                    "name": "output_binary_property",
                    "label": "Binary property",
                    "type": "string",
                    "default": "video",
                    "description": "Which binary property on the output item holds the clip.",
                },
            ],
            "credentials": [{"type": OPENROUTER_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        headers = await auth_headers(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._collect(ctx, item, headers))
        return {"main": outputs}

    async def _collect(self, ctx: ExecutionContext, item: Item, headers: dict[str, str]) -> Item:
        job_id = str(ctx.param("task_id", item=item) or "").strip()
        if not job_id:
            raise NodeConfigurationError(
                "No job id, so there is nothing to collect. Map it from the OpenRouter video "
                "submit step — {{ $json.task_id }}."
            )
        budget = _positive(ctx.param("max_wait_seconds", item=item), 900.0, "max_wait_seconds")
        interval = _positive(
            ctx.param("poll_interval_seconds", item=item), 5.0, "poll_interval_seconds"
        )
        megabytes = _positive(
            ctx.param("max_download_megabytes", item=item), 256.0, "max_download_megabytes"
        )
        binary_property = (
            str(ctx.param("output_binary_property", item=item) or "").strip() or "video"
        )

        job, polls, waited = await _poll(
            ctx, job_id, headers=headers, budget=budget, interval=interval
        )
        data, mime_type = await _download(
            ctx, job_id, headers=headers, max_bytes=int(megabytes * 1024 * 1024)
        )

        usage = _mapping(job.get("usage"))
        generation_id = str(job.get("generation_id") or item.json_.get("generation_id") or "")
        cost = await _cost(ctx, usage, generation_id.strip(), headers=headers)
        model = str(job.get("model") or item.json_.get("model") or "")
        duration = _seconds(item.json_.get("duration_seconds"))
        ctx.report_usage(
            # A generation call has no tokens; `report_usage` requires the two
            # counts anyway, so they are zero and the money rides in `cost_usd`.
            tokens_in=0,
            tokens_out=0,
            provider="openrouter",
            model=model,
            cost_usd=cost,
        )

        file_name = f"{ctx.node_id}.{'webm' if mime_type == 'video/webm' else 'mp4'}"
        ref = await ctx.put_binary(data, mime_type, file_name)
        result: dict[str, Any] = {
            "task_id": job_id,
            "generation_id": generation_id.strip(),
            "status": "completed",
            "model": model,
            "duration_seconds": duration or None,
            "video": {
                "binary_property": binary_property,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "file_name": file_name,
            },
            "usage": usage,
            "billed_seconds": duration,
            "priced": cost is not None,
            "cost_usd": str(cost) if cost is not None else "",
            "polls": polls,
            "waited_seconds": round(waited, 3),
        }
        return Item.model_validate(
            {
                "json": {**item.json_, **result},
                "binary": {**(item.binary or {}), binary_property: ref},
            }
        )


async def _poll(
    ctx: ExecutionContext,
    job_id: str,
    *,
    headers: dict[str, str],
    budget: float,
    interval: float,
) -> tuple[dict[str, Any], int, float]:
    """Query until the job is terminal or the budget runs out. Returns a completed job."""
    started = time.monotonic()
    deadline = started + budget
    wait = min(interval, _MAX_INTERVAL_SECONDS)
    polls = 0
    client = ctx.http()

    while True:
        try:
            response = await client.get(job_url(job_id), headers=headers)
        except httpx.HTTPError as error:
            raise OpenRouterUnavailable(
                f"Could not reach OpenRouter to check job {job_id} ({type(error).__name__}) — "
                "this is worth another attempt."
            ) from error
        polls += 1

        job = raise_for_video_response(
            response, action=f"status check for job {job_id}", ambiguous=False
        )
        if not isinstance(job, dict):
            raise OpenRouterError(
                f"OpenRouter answered the status check for job {job_id} with a non-object."
            )
        status = str(job.get("status") or "").strip().lower()
        if status in TERMINAL:
            _raise_unless_completed(job, job_id, status)
            return job, polls, time.monotonic() - started

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenRouterVideoNotReady(
                f"OpenRouter job {job_id} was still {status or 'unfinished'} after {budget:g}s, "
                "so this step stopped waiting. Nothing was lost and nothing was cancelled: the "
                "clip keeps generating and will still be billed, and running this step again "
                "on the same job id collects it."
            )
        await asyncio.sleep(min(wait, remaining))
        wait = min(wait * _BACKOFF_FACTOR, _MAX_INTERVAL_SECONDS)


def _raise_unless_completed(job: dict[str, Any], job_id: str, status: str) -> None:
    if status == "completed":
        return
    detail = str(job.get("error") or "").strip() or "no reason given"
    if status == "expired":
        raise OpenRouterError(
            f"OpenRouter job {job_id} expired before it could be collected ({detail}). The "
            "clip is no longer available; submit the beat again."
        )
    if status == "cancelled":
        raise OpenRouterError(
            f"OpenRouter job {job_id} was cancelled ({detail}), so there is no clip to collect."
        )
    raise OpenRouterError(
        f"OpenRouter could not generate job {job_id}: {detail}. Check openrouter.ai → Activity "
        "to see whether the attempt was charged."
    )


async def _download(
    ctx: ExecutionContext, job_id: str, *, headers: dict[str, str], max_bytes: int
) -> tuple[bytes, str]:
    """Fetch the clip from OpenRouter's content endpoint under an explicit cap."""
    try:
        response = await get_bounded(
            ctx.http(), content_url(job_id), max_bytes=max_bytes, headers=headers
        )
    except ResponseTooLargeError as error:
        raise OpenRouterError(
            f"The finished clip is larger than the {max_bytes / 1024 / 1024:g} MB this step "
            "will download, so it was abandoned part-way rather than held in memory. The clip "
            'itself is fine. Raise "Refuse a clip larger than" and run this step again, or '
            "generate at a lower resolution or a shorter duration."
        ) from error
    except httpx.HTTPError as error:
        raise OpenRouterUnavailable(
            f"Could not download the finished clip ({type(error).__name__}) — this is worth "
            "another attempt."
        ) from error
    if response.status_code == 409 or response.status_code == 429 or response.status_code >= 500:
        # 409 is OpenRouter's "resource conflict, try again later" — the job
        # reads completed but the content is not servable yet.
        raise OpenRouterUnavailable(
            f"OpenRouter answered {response.status_code} to the clip download for job {job_id} "
            "— this is worth another attempt."
        )
    if response.status_code >= 400:
        raise OpenRouterError(
            f"OpenRouter refused the clip download for job {job_id} ({response.status_code}). "
            "The job completed, so the clip was generated and charged."
        )
    data = response.content
    return data, _sniff(data, declared=str(response.headers.get("content-type", "")))


def _sniff(data: bytes, *, declared: str) -> str:
    """The container, read from the bytes — see `minimax_collect._sniff` for why."""
    if data[_MP4_BRAND_OFFSET : _MP4_BRAND_OFFSET + 4] == b"ftyp":
        return _MIME_BY_CONTAINER["mp4"]
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return _MIME_BY_CONTAINER["webm"]
    claimed = f" (it was served as {declared})" if declared.strip() else ""
    raise OpenRouterError(
        "What the clip download returned is not a video file this step recognises"
        f"{claimed} — the first bytes are neither an MP4 `ftyp` box nor a WebM header. "
        "Nothing was saved."
    )


async def _cost(
    ctx: ExecutionContext,
    usage: dict[str, Any],
    generation_id: str,
    *,
    headers: dict[str, str],
) -> Decimal | None:
    """`usage.cost` from the job, else the ledger by generation id, else unpriced."""
    stated = usage.get("cost")
    if isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated >= 0:
        return Decimal(str(stated))
    looked_up = await generation_cost(ctx, generation_id, headers=headers)
    if looked_up is None:
        return None
    return looked_up.total_cost_usd


def _mapping(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _positive(value: Any, default: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise NodeConfigurationError(f"{label} must be a number, not {value!r}.") from error
    if number <= 0:
        raise NodeConfigurationError(f"{label} must be greater than zero, not {number:g}.")
    return number
