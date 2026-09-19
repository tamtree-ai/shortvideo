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

**Image inputs go as data URIs (V2.5).** A workspace `BinaryRef` is not a
public URL, so the bytes have to travel in the request; `images.py` explains
why a bounded data URI beat a vendor upload step for v1, and carries every
check MiniMax would otherwise make after the fact. An https URL is accepted
too, and is the escape hatch when an image is too large to inline.

**Reference video and reference audio are not offered.** The create contract
takes them, and this pipeline has no use for either: §3 generates footage from
a prompt and narrates it with Wave 1's audio. Each would bring its own
container, codec, frame-rate and duration matrix to validate, which would be
surface with no caller. `callback_url` is absent for a different reason and
stays absent: MiniMax callbacks require a challenge-response endpoint Tamtree
does not have (§7).
"""

from __future__ import annotations

import json
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
from tamtree_shortvideo.images import MAX_REQUEST_BYTES, data_uri, validate_image
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

__all__ = ["MAX_REFERENCE_IMAGES", "NODE_NAME", "MinimaxSubmitNode"]

NODE_NAME: Final = "shortvideo.minimax_submit"

_DEFAULT_MODEL: Final = "MiniMax-H3"

#: The one resolution both models accept, so the default is valid whichever
#: model the author picks first.
_DEFAULT_RESOLUTION: Final = "768P"

#: MiniMax's own ceiling on `reference_image` elements in one request.
MAX_REFERENCE_IMAGES: Final = 9

#: How much of MiniMax's 64 MB request ceiling this node will fill before it
#: refuses. The margin is for the JSON structure and the prompt, and for the
#: fact that a body measured here and a body counted there need not agree to
#: the byte.
_BODY_BUDGET_BYTES: Final = 60 * 1024 * 1024

#: Schemes MiniMax resolves itself. Anything else in an image field is read as
#: the name of a binary property on the incoming item.
_PASSTHROUGH_SCHEMES: Final = ("https://", "mm_file://")


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
                            "image_roles": {"type": "array", "items": {"type": "string"}},
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
                    "name": "first_frame",
                    "label": "First frame",
                    "type": "string",
                    "default": "",
                    "description": (
                        "The image this beat starts from — the name of a binary property on "
                        "the incoming item, or an https URL. Leave empty for text-to-video. "
                        "Cannot be combined with reference images."
                    ),
                },
                {
                    "name": "last_frame",
                    "label": "Last frame",
                    "type": "string",
                    "default": "",
                    "description": (
                        "The image this beat ends on, same forms as the first frame. "
                        "Cannot be combined with reference images."
                    ),
                },
                {
                    "name": "reference_images",
                    "label": "Reference images",
                    "type": "json",
                    "default": [],
                    "description": (
                        "Up to nine images to guide style and content — a list of binary "
                        "property names or https URLs. MiniMax treats reference-to-video and "
                        "image-to-video as different jobs, so these cannot be combined with "
                        "a first or last frame."
                    ),
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

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(await _image_content(ctx, item))

        request: dict[str, Any] = {
            "model": model,
            "content": content,
            "resolution": resolution,
            "duration": duration,
            "ratio": ratio,
            "extra": {"prompt_expansion_mode": expansion},
        }
        _check_body_size(request)

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
            "image_roles": [
                element["role"] for element in content if element.get("type") == "image_url"
            ],
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


async def _image_content(ctx: ExecutionContext, item: Item) -> list[dict[str, Any]]:
    """The `content` elements for whatever images this beat carries.

    Enforces MiniMax's one structural rule *before* anything is resolved: a
    request is image-to-video or reference-to-video, never both. Discovering
    that after base64-encoding four files would be a slow way to learn it.
    """
    first_frame = _text(ctx.param("first_frame", item=item)).strip()
    last_frame = _text(ctx.param("last_frame", item=item)).strip()
    references = _reference_list(ctx.param("reference_images", item=item))

    if references and (first_frame or last_frame):
        named = " and ".join(
            label
            for label, value in (("first frame", first_frame), ("last frame", last_frame))
            if value
        )
        raise NodeConfigurationError(
            f"This step sets both reference images and a {named}. MiniMax treats "
            "image-to-video and reference-to-video as different jobs and refuses a request "
            "that asks for both — choose which one this beat is."
        )
    if len(references) > MAX_REFERENCE_IMAGES:
        raise NodeConfigurationError(
            f"This step passes {len(references)} reference images and MiniMax accepts at most "
            f"{MAX_REFERENCE_IMAGES}."
        )

    sources: list[tuple[str, str, str]] = []  # (role, value, label)
    if first_frame:
        sources.append(("first_frame", first_frame, "first frame"))
    if last_frame:
        sources.append(("last_frame", last_frame, "last frame"))
    for index, value in enumerate(references):
        sources.append(("reference_image", value, f"reference image {index + 1}"))

    return [
        {
            "type": "image_url",
            "role": role,
            "image_url": {"url": await _image_url(ctx, item, value, label=label)},
        }
        for role, value, label in sources
    ]


def _reference_list(value: Any) -> list[str]:
    """The reference list, however the `json` param arrived."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise NodeConfigurationError(
                f"Reference images is not valid JSON (line {error.lineno}, column "
                f'{error.colno}). It should be a list, like ["logo", "https://…/style.png"].'
            ) from None
    if value is None:
        return []
    if not isinstance(value, list):
        raise NodeConfigurationError(
            f"Reference images must be a list and this is a {type(value).__name__}."
        )
    entries = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise NodeConfigurationError(
                f"Reference image {index + 1} is not a binary property name or an https URL."
            )
        entries.append(entry.strip())
    return entries


async def _image_url(ctx: ExecutionContext, item: Item, value: str, *, label: str) -> str:
    """A URL MiniMax can resolve — passed through, or the attachment inlined.

    A value MiniMax already understands goes as written. Anything else names a
    binary property on this item, whose bytes are validated and inlined. `http`
    is refused on its own: the image would cross the internet in the clear on
    its way to a third party, and a host MiniMax cannot reach over TLS it
    cannot reach at all.
    """
    if value.startswith(_PASSTHROUGH_SCHEMES):
        return value
    if value.startswith("http://"):
        raise NodeConfigurationError(
            f"The {label} is a plain http URL. Use https — the image travels to MiniMax over "
            "that link, and an unencrypted one exposes it in transit."
        )
    if "://" in value:
        scheme = value.split("://", 1)[0]
        raise NodeConfigurationError(
            f"The {label} names the scheme {scheme!r}, which MiniMax cannot resolve. Use an "
            "https URL, an mm_file:// reference, or the name of a binary property on this item."
        )

    ref = (item.binary or {}).get(value)
    if ref is None:
        available = ", ".join(sorted(item.binary or {})) or "none"
        raise NodeConfigurationError(
            f"The {label} names the attachment {value!r}, and this item carries no such "
            f"binary property (it has: {available}). If you meant a URL, it must start with "
            "https://."
        )
    data = await ctx.get_binary(ref)
    fmt = validate_image(data, label=label)
    return data_uri(data, fmt)


def _check_body_size(request: dict[str, Any]) -> None:
    """Refuse a request too large to send, naming the way out.

    Measured on the assembled body rather than summed from the parts, because
    base64 and JSON escaping are what actually decide the number.
    """
    size = len(json.dumps(request).encode("utf-8"))
    if size > _BODY_BUDGET_BYTES:
        raise NodeConfigurationError(
            f"This request is {size / 1024 / 1024:.1f} MB once the images are encoded, and "
            f"MiniMax accepts at most {MAX_REQUEST_BYTES // 1024 // 1024} MB. Base64 adds "
            "about a third to every attachment. Resize the images, send fewer, or host them "
            "and pass https URLs instead of attachments."
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
