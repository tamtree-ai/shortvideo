"""`shortvideo.openrouter_video_submit` — one beat, one create call, one persisted id.

The OpenRouter twin of `minimax_submit`, and deliberately the same shape: it
turns a visual prompt into a job id Tamtree has written down, and does *not*
wait for the clip (D3 — no mid-node checkpoint, so a node that submitted and
then polled would submit again after a worker restart). `openrouter_video.py`
has the retry split and why it is identical.

**Everything checkable is checked before the request** — model, duration,
resolution, ratio, prompt length — against the matrix OpenRouter publishes.

**`generate_audio` is always false.** H3 makes its own soundtrack by default;
a short's audio is the narration and the mix `compose` builds, so a model
track would be generated, paid for and thrown away.

**No image inputs.** OpenRouter takes `frame_images`/`input_references` as
URLs a provider can fetch, and documents no inline form; a workspace
`BinaryRef` is not such a URL, and the v1 template is text-to-video. Offering
the params without a way to honour them for attachments would be surface with
no caller — `minimax_submit` remains the node for image-led beats.

**No usage is reported here.** OpenRouter states the job's cost when it
completes, so `openrouter_video_collect` reports it — one step late, the same
"± one beat" the workspace budget accepts for MiniMax.
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

from tamtree_shortvideo.credentials import OPENROUTER_CREDENTIAL_TYPE
from tamtree_shortvideo.openrouter import OpenRouterError, auth_headers
from tamtree_shortvideo.openrouter_video import (
    ASPECT_RATIOS,
    CREATE_URL,
    MAX_PROMPT_CHARACTERS,
    MODELS,
    OpenRouterVideoSubmitAmbiguous,
    VideoModel,
    raise_for_video_response,
)

__all__ = ["NODE_NAME", "OpenRouterVideoSubmitNode"]

NODE_NAME: Final = "shortvideo.openrouter_video_submit"

#: The cheaper model, at the resolution the v1 template has always rendered
#: from — H3 is 2K-only on OpenRouter, so it cannot be the 768p default.
DEFAULT_MODEL: Final = "minimax/hailuo-3-max"
DEFAULT_RESOLUTION: Final = "768p"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class OpenRouterVideoSubmitNode(ProgrammaticNode):
    """Submit one beat's visual prompt to OpenRouter and return its job id."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — OpenRouter video submit",
            "description": (
                "Start one video generation on OpenRouter, paid from your OpenRouter credits, "
                "and return its job id. Pair it with OpenRouter video collect, which waits "
                "for the clip — the split is what makes the job id survive a worker restart "
                "without paying twice."
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
                    "default": DEFAULT_MODEL,
                    "options": [
                        {"value": model_id, "label": spec.label}
                        for model_id, spec in MODELS.items()
                    ],
                    "description": (
                        "Duration and resolution limits differ by model and are checked "
                        "before the request is sent. H3 Max at 768p is the cheaper one."
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
                    "default": DEFAULT_RESOLUTION,
                    "options": [
                        {"value": "480p", "label": "480p — H3 Max only"},
                        {"value": "768p", "label": "768p — H3 Max only"},
                        {"value": "2K", "label": "2K — H3 only"},
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
                        for ratio in ASPECT_RATIOS
                    ],
                },
            ],
            "credentials": [{"type": OPENROUTER_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        headers = await auth_headers(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._submit(ctx, item, headers))
        return {"main": outputs}

    async def _submit(self, ctx: ExecutionContext, item: Item, headers: dict[str, str]) -> Item:
        model = _text(ctx.param("model", item=item)) or DEFAULT_MODEL
        prompt = _text(ctx.param("prompt", item=item)).strip()
        resolution = _resolution(_text(ctx.param("resolution", item=item)) or DEFAULT_RESOLUTION)
        ratio = _text(ctx.param("ratio", item=item)) or "9:16"
        duration = _duration(ctx.param("duration_seconds", item=item))

        spec = _model(model)
        _check_prompt(prompt)
        _check_duration(duration, model=model, spec=spec)
        _check_resolution(resolution, model=model, spec=spec)
        _check_ratio(ratio)

        request: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": ratio,
            "generate_audio": False,
        }
        payload = await _create(ctx, request, headers=headers)
        job_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(job_id, str) or not job_id.strip():
            # A 2xx with no id: a clip may be generating and billing with
            # nothing to collect it by. Named and not retried, like a 5xx.
            raise OpenRouterVideoSubmitAmbiguous(
                "OpenRouter accepted the request but returned no job id, so there is nothing "
                "to collect the clip with — and it may still be generating and billing. Check "
                "openrouter.ai → Activity before running this step again."
            )

        result: dict[str, Any] = {
            "task_id": job_id.strip(),
            "generation_id": _text(payload.get("generation_id")).strip(),
            "model": model,
            "duration_seconds": duration,
            "resolution": resolution,
            "ratio": ratio,
            "prompt": prompt,
            "submitted_at": time.time(),
        }
        return Item.model_validate(
            {
                "json": {**item.json_, **result},
                "binary": item.binary or {},
            }
        )


def _resolution(value: str) -> str:
    """OpenRouter spells `480p`/`768p` lower-case and `2K` upper-case.

    Normalised here so `768P` — `minimax_submit`'s spelling, and what an author
    moving a flow across would type — is not a refusal over a letter's case.
    """
    text = value.strip()
    return text.upper() if text.lower().endswith("k") else text.lower()


def _model(model: str) -> VideoModel:
    spec = MODELS.get(model)
    if spec is None:
        raise NodeConfigurationError(
            f"Unknown OpenRouter video model {model!r}. This plugin knows "
            f"{', '.join(sorted(MODELS))}. Another model needs a row in this plugin's matrix "
            "first — its durations and resolutions are checked locally so a wrong value fails "
            "before anything is routed."
        )
    return spec


def _duration(value: Any) -> int:
    if value is None or value == "":
        raise NodeConfigurationError(
            "Duration is required: generation is billed by the second of output, so there "
            "is no sensible default to spend on your behalf."
        )
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise NodeConfigurationError(
            f"Duration must be a whole number of seconds, and {value!r} is not a number."
        ) from None
    if seconds != int(seconds):
        raise NodeConfigurationError(
            f"OpenRouter accepts whole seconds only, and the duration is {seconds}. Round it "
            "yourself — composition trims the visual to the narration afterwards, so rounding "
            "up is usually right."
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
            f"The visual prompt is {len(prompt):,} characters and these models accept at most "
            f"{MAX_PROMPT_CHARACTERS:,}. Shorten it — a prompt this long is describing more "
            "than one shot."
        )


def _check_duration(duration: int, *, model: str, spec: VideoModel) -> None:
    if not spec.min_seconds <= duration <= spec.max_seconds:
        raise NodeConfigurationError(
            f"{model} accepts {spec.min_seconds}–{spec.max_seconds} seconds and this step "
            f"asks for {duration}. Split the beat if the narration needs longer than "
            f"{spec.max_seconds} seconds."
        )


def _check_resolution(resolution: str, *, model: str, spec: VideoModel) -> None:
    if resolution not in spec.resolutions:
        others = [name for name, other in MODELS.items() if resolution in other.resolutions]
        hint = f" {' or '.join(others)} does." if others else ""
        raise NodeConfigurationError(
            f"{model} does not generate at {resolution} on OpenRouter — it offers "
            f"{', '.join(spec.resolutions)}.{hint}"
        )


def _check_ratio(ratio: str) -> None:
    if ratio not in ASPECT_RATIOS:
        raise NodeConfigurationError(
            f"These models do not accept the aspect ratio {ratio!r}. Choose one of "
            f"{', '.join(ASPECT_RATIOS)} — {ASPECT_RATIOS[0]} is the vertical short."
        )


async def _create(
    ctx: ExecutionContext, request: dict[str, Any], *, headers: dict[str, str]
) -> dict[str, Any]:
    """The one call that can cost money, and the one place a retry is refused."""
    try:
        response = await ctx.http().post(
            CREATE_URL, json=request, headers={**headers, "Content-Type": "application/json"}
        )
    except httpx.HTTPError as error:
        raise OpenRouterVideoSubmitAmbiguous(
            f"The OpenRouter create call did not complete ({type(error).__name__}), so it is "
            "unknown whether the job was accepted — the request may have arrived and may "
            "already be billing. It is not retried automatically, because OpenRouter documents "
            "no idempotency key that could recognise a duplicate. Check openrouter.ai → "
            "Activity for a video job matching this prompt before running this step again."
        ) from error

    payload = raise_for_video_response(response, action="video generation request", ambiguous=True)
    if not isinstance(payload, dict):
        raise OpenRouterError("OpenRouter answered the video generation request with a non-object.")
    return payload
