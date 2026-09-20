"""`shortvideo.minimax_collect` — wait for one clip, then get it before the URL dies.

**Why this is its own node.** D3: `ExecutionContext` has no mid-node
checkpoint, so a single create→poll→fetch node cannot persist the `task_id`
until the whole activity succeeds — a worker restart mid-generation would
submit a second clip and bill for it twice. Splitting at the `task_id` makes
the expensive half (`minimax_submit`) run once and the free half (this) as
often as it likes. That is the whole reason the retry rules here are the
opposite of the submit node's: polling a known task costs nothing, so this
step keeps its retry budget where a create refuses to spend it.

**The download is the part with a deadline inside a deadline.** MiniMax hands
back a *time-limited* URL on a task that has already been paid for, so the
window between "succeeded" and "fetched" is the one place in this pipeline
where waiting loses something that cannot be recovered for free. The fetch
therefore happens in the same pass as the poll that saw `succeeded`, never in
a later step, and a re-run re-queries the task for a fresh URL rather than
reusing one that has since expired.

**Size.** A 15s 2K clip is plausibly larger than `SafeHttpClient`'s 25 MB
default, and that cap is a constructor argument the node context never
exposes. Contracts 1.34.0 added `get_bounded` for exactly this: one call's cap
raised, every SSRF and redirect control untouched. The cap is a param rather
than a constant because the person who knows how large their clips are is the
one running them — and it is a real cap, not an off switch: an over-size body
is abandoned mid-transfer rather than buffered and then refused.

**What it will not do is claim a cost it cannot substantiate.** MiniMax prices
a clip by the seconds it actually produced, and that figure arrives here, on
the finished task, which is why `minimax_submit` reports no usage at all. But
MiniMax publishes no per-second USD rate for the H3 models — pay-as-you-go or
contact sales — so there is no honest number to hard-code, and a node that
invented one would put a fabricated figure in a budget that stops people's
work.

**So the rate is asked for instead of assumed, on the credential.** It is a
fact about the account the key belongs to, so `minimax_api` carries it as a
required field (`credentials.py`), and this node reads it off the same payload
that authenticates the call. `price_usd_per_second` remains as a *param*, but
it now defaults to unset and means "override the credential for this step" —
for a flow that runs on a different plan, or one whose rate an operator wants
pinned in the YAML.

**A rate of `0` is still a legitimate answer** — pay-as-you-go accounts have no
contract number — but it is now one somebody chose rather than one they were
given. That matters more than the original default admitted: an unpriced
generation is invisible *twice*. `_price_usage` drops a record carrying no
tokens and no cost before it ever reaches the ledger
(`packages/engine/tamtree_engine/activities/pipeline.py:891-894 @ d73c2d3e`),
and `UNPRICED_PREDICATE` then excludes the resulting NULL-source row from
`unpriced_calls` too (`packages/server/tamtree_server/cost_bands.py:48-52 @
d73c2d3e`) — so a workspace's `unpriced_block_count` guard can never fire on
video spend, whatever it is set to. Reporting the provider's own seconds in the
output, which this node always does, is the only trace left.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
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

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE, MINIMAX_PRICE_FIELD
from tamtree_shortvideo.minimax import (
    MinimaxError,
    MinimaxNotReady,
    MinimaxUnavailable,
    headers_from,
    query_url,
    raise_for_response,
    request_id,
)
from tamtree_shortvideo.minimax_cancel import delete_task

__all__ = ["NODE_NAME", "MinimaxCollectNode"]

NODE_NAME: Final = "shortvideo.minimax_collect"

#: The statuses that end the wait. `cancelled` is terminal too — somebody
#: stopped this task, and polling it forever would hide that.
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})

#: Backoff, bounded at both ends. It grows because a clip takes minutes rather
#: than seconds and a tight poll buys nothing; it stops growing because a
#: finished clip's URL is expiring while this waits to ask again.
_BACKOFF_FACTOR: Final = 1.5
_MAX_INTERVAL_SECONDS: Final = 30.0

#: The container MiniMax returns, and the one other the sniff recognises, so a
#: surprise arrives as a named refusal rather than as bytes saved under a lie.
_MP4_BRAND_OFFSET: Final = 4
_MIME_BY_CONTAINER: Final = {"mp4": "video/mp4", "webm": "video/webm"}

#: How long the DELETE attempted on a cancelled run is allowed to take. The
#: run is already ending; a cleanup that hangs would turn a cancel into a
#: worker stuck on a socket.
_CANCEL_GRACE_SECONDS: Final = 10.0


class MinimaxCollectNode(ProgrammaticNode):
    """Poll one MiniMax task to a terminal state and save what it produced."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — MiniMax collect",
            "description": (
                "Wait for a MiniMax generation to finish and save the clip as an "
                "attachment. Takes the task id from the submit step, polls until the "
                "task is done, and downloads the result before its URL expires."
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
                            "status": {"type": "string"},
                            "model": {"type": "string"},
                            "resolution": {"type": "string"},
                            "ratio": {"type": "string"},
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
                            "created_at": {"type": "integer"},
                            "updated_at": {"type": "integer"},
                        },
                        "required": ["task_id", "status", "video"],
                    },
                }
            ],
            "params": [
                {
                    "name": "task_id",
                    "label": "Task id",
                    "type": "string",
                    "default": "",
                    "required": True,
                    "description": (
                        "The id MiniMax submit returned. Map it from that step — "
                        "{{ $json.task_id }}. MiniMax keeps a task queryable for 7 days."
                    ),
                },
                {
                    "name": "max_wait_seconds",
                    "label": "Give up waiting after (seconds)",
                    "type": "number",
                    "default": 900,
                    "description": (
                        "The whole budget for the wait, not one poll. Generation takes "
                        "minutes, so this is deliberately generous; running out does not "
                        "cancel or lose the clip — the task keeps going and this step can "
                        "be run again on the same task id to collect it."
                    ),
                },
                {
                    "name": "poll_interval_seconds",
                    "label": "First poll interval (seconds)",
                    "type": "number",
                    "default": 5,
                    "description": (
                        "The gap before the first re-check. It grows by half each time, "
                        "up to 30s — a tight poll buys nothing on a job that takes minutes, "
                        "but the gap cannot grow without bound because the finished clip's "
                        "URL starts expiring the moment it exists."
                    ),
                },
                {
                    "name": "max_download_megabytes",
                    "label": "Refuse a clip larger than (MB)",
                    "type": "number",
                    "default": 256,
                    "description": (
                        "A real cap: an over-size clip is abandoned mid-download rather "
                        "than buffered and then refused. The default is well above a 15s "
                        "2K clip and well below anything that would trouble a worker."
                    ),
                },
                {
                    "name": "output_binary_property",
                    "label": "Binary property",
                    "type": "string",
                    "default": "video",
                    "description": "Which binary property on the output item holds the clip.",
                },
                {
                    "name": "price_usd_per_second",
                    "label": "Override the rate, in USD per generated second",
                    "type": "number",
                    "description": (
                        "Leave this empty and the rate comes from the MiniMax credential, "
                        "where it was set once for the whole account. Fill it in only to "
                        "price this step differently — a flow running on another plan, or "
                        "a rate you want pinned in the flow itself. 0 here reports the clip "
                        "as unpriced: MiniMax's own seconds still appear in the output, but "
                        "no cost reaches the workspace budget and none is counted against "
                        "its unpriced-spend guard either."
                    ),
                },
            ],
            "credentials": [{"type": MINIMAX_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        # One credential fetch for the whole step: the payload carries both the
        # token that authenticates the poll and the rate that prices the clip.
        credential = await ctx.credential(MINIMAX_CREDENTIAL_TYPE)
        headers = headers_from(credential)
        account_rate = _credential_rate(credential)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._collect(ctx, item, headers, account_rate))
        return {"main": outputs}

    async def _collect(
        self,
        ctx: ExecutionContext,
        item: Item,
        headers: dict[str, str],
        account_rate: Decimal,
    ) -> Item:
        task_id = str(ctx.param("task_id", item=item) or "").strip()
        if not task_id:
            raise NodeConfigurationError(
                "No task id, so there is nothing to collect. Map it from the MiniMax "
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
        rate = _rate(ctx.param("price_usd_per_second", item=item), account_rate)

        task, polls, waited = await _poll(
            ctx, task_id, headers=headers, budget=budget, interval=interval
        )
        url = _result_url(task, task_id)
        data, mime_type = await _download(ctx, url, max_bytes=int(megabytes * 1024 * 1024))

        usage = _mapping(task.get("usage"))
        billed = _billed_seconds(usage, task)
        cost = (
            (Decimal(str(billed)) * rate).quantize(Decimal("0.00000001"))
            if rate > 0 and billed > 0
            else None
        )
        ctx.report_usage(
            # A generation call has no tokens; `report_usage` requires the two
            # counts anyway (§5.2), so they are zero and the money — when there
            # is a rate to compute it from — rides in `cost_usd`.
            tokens_in=0,
            tokens_out=0,
            provider="minimax",
            model=str(task.get("model") or ""),
            cost_usd=cost,
        )

        file_name = f"{ctx.node_id}.{'webm' if mime_type == 'video/webm' else 'mp4'}"
        ref = await ctx.put_binary(data, mime_type, file_name)
        result: dict[str, Any] = {
            "task_id": task_id,
            "status": str(task.get("status") or ""),
            "model": str(task.get("model") or ""),
            "resolution": str(task.get("resolution") or ""),
            "ratio": str(task.get("ratio") or ""),
            "duration_seconds": task.get("duration"),
            "video": {
                "binary_property": binary_property,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "file_name": file_name,
            },
            "usage": usage,
            "billed_seconds": billed,
            "priced": cost is not None,
            "cost_usd": str(cost) if cost is not None else "",
            "polls": polls,
            "waited_seconds": round(waited, 3),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }
        return Item.model_validate(
            {
                # The beat's own json — narration, prompt, captions — travels on
                # with its footage, which is what makes the Loop body composable.
                "json": {**item.json_, **result},
                # §9's BinaryRef preservation: the narration stays, the clip joins it.
                "binary": {**(item.binary or {}), binary_property: ref},
            }
        )


async def _poll(
    ctx: ExecutionContext,
    task_id: str,
    *,
    headers: dict[str, str],
    budget: float,
    interval: float,
) -> tuple[dict[str, Any], int, float]:
    """Query until the task is terminal, the budget runs out, or the run is cancelled.

    Returns the succeeded task. Every other ending raises, because there is no
    honest item to emit for a clip that does not exist.
    """
    started = time.monotonic()
    deadline = started + budget
    wait = min(interval, _MAX_INTERVAL_SECONDS)
    polls = 0
    client = ctx.http()

    while True:
        try:
            response = await client.get(query_url(task_id), headers=headers)
        except httpx.HTTPError as error:
            # Unlike a create, a poll costs nothing and creates nothing, so an
            # unreachable provider keeps its retry budget rather than becoming
            # a named refusal a person has to adjudicate.
            raise MinimaxUnavailable(
                f"Could not reach MiniMax to check {task_id} ({type(error).__name__}) — "
                "this is worth another attempt."
            ) from error
        except asyncio.CancelledError:
            await _stop_provider_side(ctx, task_id, headers=headers)
            raise
        polls += 1

        payload = raise_for_response(
            response, action=f"status check for {task_id}", ambiguous=False
        )
        task = payload.get("task") if isinstance(payload, dict) else None
        if not isinstance(task, dict):
            trace = request_id(payload, response)
            raise MinimaxError(
                f"MiniMax answered the status check for {task_id} without a task"
                f"{f' (request id {trace})' if trace else ''}."
            )
        status = str(task.get("status") or "").strip().lower()
        if status in TERMINAL:
            _raise_unless_succeeded(task, task_id, status)
            return task, polls, time.monotonic() - started

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MinimaxNotReady(
                f"MiniMax task {task_id} was still {status or 'unfinished'} after "
                f"{budget:g}s, so this step stopped waiting. Nothing was lost and nothing "
                "was cancelled: the clip keeps generating and will still be billed, and "
                "running this step again on the same task id collects it. MiniMax keeps a "
                "task queryable for 7 days."
            )
        try:
            await asyncio.sleep(min(wait, remaining))
        except asyncio.CancelledError:
            await _stop_provider_side(ctx, task_id, headers=headers)
            raise
        wait = min(wait * _BACKOFF_FACTOR, _MAX_INTERVAL_SECONDS)


def _raise_unless_succeeded(task: dict[str, Any], task_id: str, status: str) -> None:
    """Terminal and not succeeded: say what MiniMax said, in MiniMax's words.

    The failure body is `error: {code, message}` — a different shape from the
    HTTP error envelope `raise_for_response` reads, because this arrived on a
    200: the *call* worked, the *generation* did not.
    """
    if status == "succeeded":
        return
    if status == "cancelled":
        raise MinimaxError(
            f"MiniMax task {task_id} was cancelled, so there is no clip to collect. "
            "Whether it was billed depends on how far it got: a task cancelled while "
            "still queued is not charged, one deleted after it finished already was."
        )
    error = _mapping(task.get("error"))
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    detail = " — ".join(part for part in (code, message) if part) or "no reason given"
    raise MinimaxError(
        f"MiniMax could not generate task {task_id}: {detail}. The clip was attempted, "
        "so check your MiniMax usage before assuming it was free."
    )


def _result_url(task: dict[str, Any], task_id: str) -> str:
    content = _mapping(task.get("content"))
    url = str(content.get("url") or "").strip()
    if not url:
        raise MinimaxError(
            f"MiniMax reported task {task_id} as succeeded but returned no download URL, "
            "so the clip cannot be fetched. It was generated and billed — it is in the "
            "MiniMax task list."
        )
    if not url.lower().startswith("https://"):
        # The clip is a paid artifact fetched from a signed URL; plain http
        # would carry it, and the signature, in the clear.
        raise MinimaxError(
            f"MiniMax returned a non-https download URL for task {task_id}, which this "
            "step will not fetch over."
        )
    return url


async def _download(ctx: ExecutionContext, url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """Fetch the clip under an explicit cap, and check it is one.

    `get_bounded` (contracts 1.34.0) is what makes the cap per-call: on the
    `SafeHttpClient` a node is handed this still runs the scheme allowlist,
    egress allowlist, DNS pinning, private-range denial and per-hop redirect
    re-validation — only the size limit differs from every other call.
    """
    try:
        response = await get_bounded(ctx.http(), url, max_bytes=max_bytes)
    except ResponseTooLargeError as error:
        # Named rather than left as the SDK's ValueError, because the fix is a
        # setting on this step and the person reading the banner is the one who
        # can change it. The clip exists and was billed either way.
        raise MinimaxError(
            f"The finished clip is larger than the {max_bytes / 1024 / 1024:g} MB this "
            "step will download, so it was abandoned part-way rather than held in "
            "memory. The clip itself is fine — it is in the MiniMax task list. Raise "
            '"Refuse a clip larger than" and run this step again, or generate at a '
            "lower resolution or a shorter duration."
        ) from error
    except httpx.HTTPError as error:
        raise MinimaxUnavailable(
            f"Could not download the finished clip ({type(error).__name__}) — this is "
            "worth another attempt, and a re-run re-queries the task for a fresh URL."
        ) from error
    if response.status_code >= 400:
        # The usual cause is an expired signature: the task succeeded minutes
        # ago and the URL it came with has lapsed. Re-running re-queries.
        raise MinimaxUnavailable(
            f"The clip's download URL answered {response.status_code}. These URLs are "
            "time-limited — run this step again and it will ask MiniMax for a fresh one."
        )
    data = response.content
    return data, _sniff(data, declared=str(response.headers.get("content-type", "")))


def _sniff(data: bytes, *, declared: str) -> str:
    """The container, read from the bytes rather than from the header.

    A `Content-Type` is whatever the CDN in front of the file was configured to
    say; the bytes are what the compositor in Wave 3 has to open. Saving an
    error page under `video/mp4` because a header claimed so is the failure
    this exists to prevent — and it is the same reasoning `images.py` applies
    to the input side.
    """
    if data[_MP4_BRAND_OFFSET : _MP4_BRAND_OFFSET + 4] == b"ftyp":
        return _MIME_BY_CONTAINER["mp4"]
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return _MIME_BY_CONTAINER["webm"]
    claimed = f" (it was served as {declared})" if declared.strip() else ""
    raise MinimaxError(
        "What the download URL returned is not a video file this step recognises"
        f"{claimed} — the first bytes are neither an MP4 `ftyp` box nor a WebM header. "
        "Nothing was saved, because saving it under a video MIME type would only move "
        "the failure into whatever opens it next."
    )


async def _stop_provider_side(
    ctx: ExecutionContext, task_id: str, *, headers: dict[str, str]
) -> None:
    """V2.4's other call site: the run was cancelled, so stop waiting and try the DELETE.

    Best-effort on purpose. The local wait has already stopped — that is what
    `CancelledError` means and it is re-raised untouched by the caller — and a
    cleanup that raises or hangs would turn a cancelled run into a stuck
    worker. The DELETE gets one bounded attempt and its outcome is swallowed,
    which is honest rather than lax: MiniMax refuses to cancel a task that is
    already running, so the common answer here is "no", and there is no output
    item left on a cancelled run to report it in. The provider's own task list
    is the record, and `shortvideo.minimax_cancel` is the author-reachable
    version of this same operation for a path that is *not* being torn down.

    Nothing here claims the clip stopped. V2.4's sentence — "local wait
    stopped; provider generation may continue and may be billed" — is the
    accurate description of a cancelled run, and `minimax_cancel.RUNNING_NOTE`
    is where it is stated for a caller that has somewhere to put it.
    """
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(
            asyncio.shield(asyncio.ensure_future(delete_task(ctx, task_id, headers=headers))),
            timeout=_CANCEL_GRACE_SECONDS,
        )


def _mapping(value: Any) -> dict[str, Any]:
    """A sub-object from the task, or an empty one.

    MiniMax omits `content` on a task that has not succeeded, `error` on one
    that has, and `usage` on both — so every read of a nested object here is
    conditional, and doing it in one place keeps the call sites about what
    they mean rather than about what might be missing.
    """
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


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


def _rate(value: Any, account_rate: Decimal) -> Decimal:
    """The param's override, or the account's rate when the param is unset.

    Empty means *unset*, not zero — zero is a rate somebody typed, and the two
    have to stay distinguishable or the override could never be left out.
    """
    if value is None or value == "":
        return account_rate
    return _decimal(value, source="price_usd_per_second on this step")


def _credential_rate(payload: Mapping[str, str]) -> Decimal:
    """The account rate off the `minimax_api` credential.

    Required on the credential type, so a missing one means a payload stored
    before that field existed — named here rather than silently treated as 0,
    because "unpriced" must always be a choice somebody made.
    """
    raw = payload.get(MINIMAX_PRICE_FIELD)
    if raw is None or str(raw).strip() == "":
        raise NodeConfigurationError(
            f"The {MINIMAX_CREDENTIAL_TYPE!r} credential has no "
            f"{MINIMAX_PRICE_FIELD!r}. Open the credential and enter the per-second "
            "rate on your MiniMax plan, so video spend reaches the workspace budget. "
            "Enter 0 if your plan has no fixed rate and you accept that these "
            "generations stay invisible to the budget and to its unpriced-spend guard."
        )
    return _decimal(
        raw, source=f"{MINIMAX_PRICE_FIELD} on the {MINIMAX_CREDENTIAL_TYPE} credential"
    )


def _decimal(value: Any, *, source: str) -> Decimal:
    try:
        rate = Decimal(str(value).strip())
    except Exception as error:  # noqa: BLE001 - Decimal raises InvalidOperation, not ValueError
        raise NodeConfigurationError(f"{source} must be a number, not {value!r}.") from error
    if rate < 0:
        raise NodeConfigurationError(
            f"{source} cannot be negative, and {rate} is. Use 0 to report the clip as unpriced."
        )
    return rate


def _billed_seconds(usage: dict[str, Any], task: dict[str, Any]) -> float:
    """What MiniMax says it billed, preferring its own total over the request's.

    `total_seconds` is the provider's figure and the only one that reflects
    what actually happened; `duration` is what was *asked for*. They agree on a
    clean generation and the total is what matters when they do not.
    """
    for key in ("total_seconds", "output_seconds"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    duration = task.get("duration")
    return float(duration) if isinstance(duration, (int, float)) and duration > 0 else 0.0
