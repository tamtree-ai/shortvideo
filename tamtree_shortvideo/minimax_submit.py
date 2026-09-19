"""`shortvideo.minimax_submit` — one beat, one create call, one persisted id.

The whole job of this node is to turn a visual prompt into a `task_id` that
Tamtree has written down. It deliberately does *not* wait for the clip: D3 —
`ExecutionContext` has no mid-node checkpoint, so a node that submitted and
then polled could not persist the id until the entire activity succeeded, and
a worker restart in the middle would submit a second time and bill a second
time. Splitting the step is what makes the id durable.

**Everything checkable is checked before the request.** The model/duration/
resolution matrix, the ratio, the prompt length — each is a local refusal
rather than a billed rejection. A 400 from MiniMax costs nothing directly, but
it costs a run, and the author reading the banner learns more from "MiniMax-H3
accepts 4 to 15 seconds" than from `invalid_params`.

**What this node does not do yet.** Image and reference inputs (`first_frame`,
`last_frame`, `reference_image`) are V2.5: a workspace `BinaryRef` is not a
public URL, and deciding between bounded data URIs and a vendor upload step is
a real decision with its own size, format and egress rules. Rather than half of
it, this node submits text-to-video and the parameters arrive with the transport
that makes them work. `callback_url` is also absent, and stays absent: MiniMax
callbacks require a challenge-response endpoint Tamtree does not have (§7).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Final

import httpx
from tamtree_plugin_sdk import (
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
)

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE
from tamtree_shortvideo.minimax import (
    CREATE_URL,
    MAX_PROMPT_CHARACTERS,
    MODELS,
    RATIOS,
    MinimaxError,
    MinimaxSubmitAmbiguous,
    auth_headers,
    raise_for_response,
    request_id,
)

__all__ = ["NODE_NAME", "MinimaxSubmitNode"]

NODE_NAME: Final = "shortvideo.minimax_submit"

_DEFAULT_MODEL: Final = "MiniMax-H3"

#: The one resolution both models accept, so the default is valid whichever
#: model the author picks first.
_DEFAULT_RESOLUTION: Final = "768P"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class MinimaxSubmitNode(ProgrammaticNode):
    """Submit one beat's visual prompt and return its `task_id`."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — MiniMax submit",
            "description": (
                "Start one MiniMax video generation and return its task id. Pair it with "
                "MiniMax collect, which waits for the clip — the split is what makes the "
                "task id survive a worker restart without paying twice."
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
                            "request_id": {"type": "string"},
                            "model": {"type": "string"},
                            "duration_seconds": {"type": "number"},
                            "resolution": {"type": "string"},
                            "ratio": {"type": "string"},
                            "prompt": {"type": "string"},
                            "submitted_at": {"type": "number"},
                        },
                        "required": ["task_id", "model", "duration_seconds", "resolution"],
                    },
                }
            ],
            "params": [
                {
                    "name": "model",
                    "label": "Model",
                    "type": "options",
                    "default": _DEFAULT_MODEL,
                    "options": [
                        {"value": "MiniMax-H3", "label": "MiniMax-H3 — 4–15s, 768P or 2K"},
                        {
                            "value": "MiniMax-H3-Max",
                            "label": "MiniMax-H3-Max — 5–15s, 480P or 768P, faster",
                        },
                    ],
                    "description": (
                        "Duration and resolution limits differ by model and are checked "
                        "before the request is sent."
                    ),
                },
                {
                    "name": "prompt",
                    "label": "Visual prompt",
                    "type": "string",
                    "default": "",
                    "required": True,
                    "description": (
                        "What this beat should show. Map it from the shot list. "
                        f"At most {MAX_PROMPT_CHARACTERS:,} characters."
                    ),
                },
                {
                    "name": "duration_seconds",
                    "label": "Duration (seconds)",
                    "type": "number",
                    "default": 6,
                    "description": (
                        "Whole seconds. Generation is billed per second of output, so this "
                        "is the main thing that decides what a short costs. Round up to the "
                        "narration length — composition trims the visual back down."
                    ),
                },
                {
                    "name": "resolution",
                    "label": "Resolution",
                    "type": "options",
                    "default": _DEFAULT_RESOLUTION,
                    "options": [
                        {"value": "480P", "label": "480P — MiniMax-H3-Max only"},
                        {"value": "768P", "label": "768P — both models"},
                        {"value": "2K", "label": "2K — MiniMax-H3 only"},
                    ],
                },
                {
                    "name": "ratio",
                    "label": "Aspect ratio",
                    "type": "options",
                    "default": "9:16",
                    "options": [
                        {
                            "value": ratio,
                            "label": "9:16 — vertical short" if ratio == "9:16" else ratio,
                        }
                        for ratio in RATIOS
                    ],
                },
                {
                    "name": "prompt_expansion_mode",
                    "label": "Prompt expansion",
                    "type": "options",
                    "default": "balanced",
                    "options": [
                        {"value": "balanced", "label": "Balanced — MiniMax's default"},
                        {"value": "disabled", "label": "Disabled — use the prompt as written"},
                        {"value": "quality", "label": "Quality"},
                    ],
                    "description": (
                        "How much MiniMax rewrites the prompt before generating. Disable it "
                        "when the shot list is already specific and you want beats to match."
                    ),
                },
            ],
            "credentials": [{"type": MINIMAX_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        headers = await auth_headers(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._submit(ctx, item, headers))
        return {"main": outputs}

    async def _submit(self, ctx: ExecutionContext, item: Item, headers: dict[str, str]) -> Item:
        model = _text(ctx.param("model", item=item)) or _DEFAULT_MODEL
        prompt = _text(ctx.param("prompt", item=item)).strip()
        resolution = (_text(ctx.param("resolution", item=item)) or _DEFAULT_RESOLUTION).upper()
        ratio = _text(ctx.param("ratio", item=item)) or "9:16"
        expansion = _text(ctx.param("prompt_expansion_mode", item=item)) or "balanced"
        duration = _duration(ctx.param("duration_seconds", item=item))

        # Every one of these is free to refuse and costs a run to discover.
        limits = _model_limits(model)
        _check_prompt(prompt)
        _check_duration(duration, model=model, limits=limits)
        _check_resolution(resolution, model=model, limits=limits)
        _check_ratio(ratio)

        request: dict[str, Any] = {
            "model": model,
            "content": [{"type": "text", "text": prompt}],
            "resolution": resolution,
            "duration": duration,
            "ratio": ratio,
            "extra": {"prompt_expansion_mode": expansion},
        }

        payload = await _create(ctx, request, headers=headers)
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        trace = request_id(payload)
        if not isinstance(task_id, str) or not task_id.strip():
            # A 2xx with no id is the worst answer available: a clip may be
            # generating and billing with nothing to collect it by. Named, and
            # not retried, for exactly the reason a 5xx is not.
            raise MinimaxSubmitAmbiguous(
                "MiniMax accepted the request but returned no task id"
                f"{f' (request id {trace})' if trace else ''}, so there is nothing to collect "
                "the clip with — and it may still be generating and billing. Check the task "
                "list before running this step again."
            )

        result: dict[str, Any] = {
            "task_id": task_id.strip(),
            "request_id": trace,
            "model": model,
            "duration_seconds": duration,
            "resolution": resolution,
            "ratio": ratio,
            "prompt": prompt,
            "submitted_at": time.time(),
        }
        # Usage is deliberately not reported here: MiniMax prices a clip by the
        # seconds it actually produced, and that figure arrives with the
        # finished task. `minimax_collect` reports it from `task.usage`, which
        # is the provider's own number rather than an estimate from what was
        # asked for (§5.2 / the SDK's T6). The cost therefore reaches the
        # ledger one step late — which is precisely the "± one beat" the
        # workspace budget was accepted as being accurate to.
        return Item.model_validate(
            {
                "json": {**item.json_, **result},
                "binary": item.binary or {},
            }
        )


def _model_limits(model: str) -> Any:
    limits = MODELS.get(model)
    if limits is None:
        raise NodeConfigurationError(
            f"Unknown MiniMax model {model!r}. This plugin knows "
            f"{', '.join(sorted(MODELS))}. If MiniMax has released a new one, the model "
            "matrix in this plugin needs updating — its duration and resolution limits are "
            "checked locally so a wrong value fails before it is billed."
        )
    return limits


def _duration(value: Any) -> int:
    """Whole seconds. MiniMax's `duration` is an integer, and 6.5 would be
    rounded by someone — better here, loudly, than silently by a gateway."""
    if value is None or value == "":
        raise NodeConfigurationError(
            "Duration is required: MiniMax bills by the second of output, so there is no "
            "sensible default to spend on your behalf."
        )
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise NodeConfigurationError(
            f"Duration must be a whole number of seconds, and {value!r} is not a number."
        ) from None
    if seconds != int(seconds):
        raise NodeConfigurationError(
            f"MiniMax accepts whole seconds only, and the duration is {seconds}. Round it "
            "yourself rather than letting this step guess which way — composition trims the "
            "visual to the narration afterwards, so rounding up is usually right."
        )
    return int(seconds)


def _check_prompt(prompt: str) -> None:
    if not prompt:
        raise NodeConfigurationError(
            "The visual prompt is empty, so there is nothing to generate. Map it from the "
            "shot list's visual prompt for this beat."
        )
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        raise NodeConfigurationError(
            f"The visual prompt is {len(prompt):,} characters and MiniMax accepts at most "
            f"{MAX_PROMPT_CHARACTERS:,}. Shorten it — a prompt this long is describing more "
            "than one shot."
        )


def _check_duration(duration: int, *, model: str, limits: Any) -> None:
    if not limits.min_seconds <= duration <= limits.max_seconds:
        raise NodeConfigurationError(
            f"{model} accepts {limits.min_seconds}–{limits.max_seconds} seconds and this step "
            f"asks for {duration}. Split the beat if the narration needs longer than "
            f"{limits.max_seconds} seconds."
        )


def _check_resolution(resolution: str, *, model: str, limits: Any) -> None:
    if resolution not in limits.resolutions:
        others = [name for name, spec in MODELS.items() if resolution in spec.resolutions]
        hint = f" {' or '.join(others)} does." if others else ""
        raise NodeConfigurationError(
            f"{model} does not generate at {resolution} — it offers "
            f"{', '.join(limits.resolutions)}.{hint}"
        )


def _check_ratio(ratio: str) -> None:
    if ratio not in RATIOS:
        raise NodeConfigurationError(
            f"MiniMax does not accept the aspect ratio {ratio!r}. Choose one of "
            f"{', '.join(RATIOS)} — {RATIOS[0]} is the vertical short."
        )


async def _create(
    ctx: ExecutionContext, request: dict[str, Any], *, headers: dict[str, str]
) -> Any:
    """The one call that can cost money, and the one place a retry is refused.

    See `minimax.py`: a transport failure on a create is *ambiguous*, not
    transient. The task may exist. Retrying automatically would charge twice
    for a clip nobody asked for twice, so the error is named and a person
    decides.
    """
    try:
        response = await ctx.http().post(CREATE_URL, json=request, headers=headers)
    except httpx.HTTPError as error:
        raise MinimaxSubmitAmbiguous(
            f"The MiniMax create call did not complete ({type(error).__name__}), so it is "
            "unknown whether the clip was accepted — the request may have arrived and may "
            "already be billing. It is not retried automatically, because MiniMax documents "
            "no idempotency key that could recognise a duplicate. Check the task list for a "
            "clip matching this prompt before running this step again."
        ) from error

    payload = raise_for_response(response, action="video generation request", ambiguous=True)
    if not isinstance(payload, dict):
        raise MinimaxError("MiniMax answered the video generation request with a non-object.")
    return payload
