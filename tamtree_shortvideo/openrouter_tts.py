"""`shortvideo.openrouter_tts` — narration via Gemini 3.1 Flash TTS, with no
service account and no provider-side caption marks to lean on.

**Why this node exists next to `google_tts` rather than replacing it.** An
operator without a GCP project can still narrate a short; one who already has
exact per-mark timing from Google loses nothing by that node staying exactly
as it is. The two share an output shape (`duration_seconds`, `marks`, `audio`,
`captions`) so the compositor cannot tell which one produced an item (D9), but
they get there differently — this is the difference the rest of this docstring
is about.

**What `google_tts` has that this node cannot ask OpenRouter for.** Google's
`text:synthesize` takes SSML and returns exact `<mark>` timepoints in the same
call — one request buys audio and caption timing together. OpenRouter's
`/api/v1/audio/speech` takes plain text (bracketed delivery tags, not SSML)
and returns nothing but audio bytes: no marks, no timepoints, no way to ask
for them. Dropping per-phrase captions entirely was the alternative already
weighed and rejected — see this subproject's handover for the trade-off.

**The answer: one call per phrase, stitched.** In `captions` mode, each phrase
is synthesized on its own, so its exact duration is known the same way
`google_tts`'s duration is known — measured from the bytes, not estimated.
Concatenating those in order, with a small artificial gap between them,
produces both the audio and a caption track whose timings are exact, without
ever needing the provider to hand back a mark. `pcm.py` is what makes the
concatenation itself trivial: PCM, not MP3, so joining chunks is `b"".join`.

**What is genuinely different from `google_tts`'s output, not just missing.**
Each phrase is now its own isolated utterance rather than one continuous
reading — the prosody a single-pass narrator gives a sentence mid-paragraph
(rising into it, trailing out) is not what N independent one-sentence
syntheses sound like stitched together. `phrase_gap_seconds` exists to soften
the seam, not to hide that there is one. This is a real, audible difference
worth expecting, not a bug in the stitching.

**Why cost comes from a lookup and not a param.** See `openrouter.py`'s module
docstring — `total_cost` on the generation endpoint is a real number from
OpenRouter's own ledger, not a rate this node would otherwise have to ask the
operator to keep current.
"""

from __future__ import annotations

from decimal import Decimal
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
from tamtree_shortvideo.openrouter import (
    MODEL_ID,
    SPEECH_URL,
    GenerationCost,
    OpenRouterError,
    OpenRouterUnavailable,
    auth_headers,
    generation_cost,
    raise_for_response,
)
from tamtree_shortvideo.pcm import pcm_duration_seconds, silence, wrap_pcm_as_wav
from tamtree_shortvideo.ssml import SsmlPhrase, parse_phrases

__all__ = ["NODE_NAME", "OpenRouterTtsNode"]

NODE_NAME: Final = "shortvideo.openrouter_tts"

#: One of the voices OpenRouter's model page lists at time of writing. Not
#: authoritative — check openrouter.ai for the current set before relying on
#: this default in production.
_DEFAULT_VOICE: Final = "Zephyr"

_DEFAULT_PHRASE_GAP_SECONDS: Final = 0.15


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any, *, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise NodeConfigurationError(
            f"{name!r} must be a number, and {value!r} is not one."
        ) from None


class OpenRouterTtsNode(ProgrammaticNode):
    """Synthesize one narration track per item through Gemini 3.1 Flash TTS."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — OpenRouter Text-to-Speech (Gemini 3.1 Flash)",
            "description": (
                "Synthesize narration with Gemini 3.1 Flash TTS via OpenRouter. No Google "
                "service account required. Returns the audio as an attachment and its measured "
                "duration; in Phrase list mode, also returns exact per-phrase caption timings, "
                "built by synthesizing and stitching one call per phrase rather than from "
                "provider-returned marks (OpenRouter's TTS endpoint has no equivalent)."
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
                            "duration_seconds": {"type": "number"},
                            "marks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "time_seconds": {"type": "number"},
                                    },
                                    "required": ["name", "time_seconds"],
                                },
                            },
                            "audio": {
                                "type": "object",
                                "properties": {
                                    "binary_property": {"type": "string"},
                                    "encoding": {"type": "string"},
                                    "mime_type": {"type": "string"},
                                    "sample_rate_hertz": {"type": "number"},
                                    "size_bytes": {"type": "number"},
                                    "file_name": {"type": "string"},
                                },
                                "required": ["binary_property", "encoding", "size_bytes"],
                            },
                            "voice": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            },
                            "captions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "text": {"type": "string"},
                                        "start_seconds": {"type": "number"},
                                        "end_seconds": {"type": "number"},
                                    },
                                    "required": ["name", "text", "start_seconds", "end_seconds"],
                                },
                            },
                            "usage": {
                                "type": "object",
                                "properties": {
                                    "calls": {"type": "number"},
                                    "tokens_prompt": {"type": "number"},
                                    "tokens_completion": {"type": "number"},
                                },
                                "required": ["calls", "tokens_prompt", "tokens_completion"],
                            },
                            "priced": {"type": "boolean"},
                            "cost_usd": {"type": "string"},
                            "input_mode": {"type": "string"},
                        },
                        "required": [
                            "duration_seconds",
                            "marks",
                            "audio",
                            "voice",
                            "usage",
                            "priced",
                        ],
                    },
                }
            ],
            "params": [
                {
                    "name": "input_mode",
                    "label": "Input",
                    "type": "options",
                    "default": "text",
                    "options": [
                        {"value": "text", "label": "Plain text"},
                        {
                            "value": "captions",
                            "label": "Phrase list — exact per-phrase caption timings",
                        },
                    ],
                    "description": (
                        "Plain text makes one call for the whole block — fast, but no "
                        "per-phrase caption timing. A phrase list makes one call per phrase and "
                        "stitches them, which is slower and costs one call each, but returns "
                        "exact caption timings for every phrase. There is no SSML mode: this "
                        "provider does not accept SSML."
                    ),
                },
                {
                    "name": "text",
                    "label": "Text",
                    "type": "string",
                    "default": "",
                    "description": "The narration to speak, as plain text.",
                    "display_options": {"show": {"input_mode": ["text"]}},
                },
                {
                    "name": "captions",
                    "label": "Phrases",
                    "type": "json",
                    "default": [],
                    "description": (
                        'A list of phrases — ["First line.", "Second line."] — or objects '
                        'like {"text": "…", "name": "own-mark-name"}. One synthesis call per '
                        "phrase, spoken in order and stitched together."
                    ),
                    "display_options": {"show": {"input_mode": ["captions"]}},
                },
                {
                    "name": "phrase_gap_seconds",
                    "label": "Gap between phrases (seconds)",
                    "type": "number",
                    "default": _DEFAULT_PHRASE_GAP_SECONDS,
                    "description": (
                        "Silence inserted between independently-synthesized phrases. Each "
                        "phrase is its own isolated utterance — this softens the seam but does "
                        "not reproduce one continuous reading's prosody."
                    ),
                    "display_options": {"show": {"input_mode": ["captions"]}},
                },
                {
                    "name": "voice",
                    "label": "Voice",
                    "type": "string",
                    "default": _DEFAULT_VOICE,
                    "description": (
                        "A voice name from OpenRouter's current Gemini 3.1 Flash TTS voice "
                        "list (check openrouter.ai — the default here is not authoritative)."
                    ),
                },
                {
                    "name": "output_binary_property",
                    "label": "Attachment name",
                    "type": "string",
                    "default": "audio",
                    "description": "Which binary property on the output item holds the audio.",
                },
            ],
            "credentials": [{"type": OPENROUTER_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        headers = await auth_headers(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._synthesize(ctx, item, headers))
        return {"main": outputs}

    async def _synthesize(self, ctx: ExecutionContext, item: Item, headers: dict[str, str]) -> Item:
        mode = (_text(ctx.param("input_mode", item=item)) or "text").lower()
        voice = _text(ctx.param("voice", item=item)).strip() or _DEFAULT_VOICE
        binary_property = _text(ctx.param("output_binary_property", item=item)) or "audio"

        if mode == "text":
            pcm, marks, captions, calls = await self._synthesize_text(ctx, item, voice, headers)
        elif mode == "captions":
            pcm, marks, captions, calls = await self._synthesize_captions(ctx, item, voice, headers)
        else:
            raise NodeConfigurationError(
                f"Unknown input mode {mode!r} — choose 'text' or 'captions'."
            )

        duration = pcm_duration_seconds(pcm)
        wav = wrap_pcm_as_wav(pcm)
        known_costs = [call.total_cost_usd for call in calls if call.total_cost_usd is not None]
        total_cost = sum(known_costs, Decimal("0")) if len(known_costs) == len(calls) else None
        tokens_prompt = sum(call.tokens_prompt for call in calls)
        tokens_completion = sum(call.tokens_completion for call in calls)

        file_name = f"{ctx.node_id}.wav"
        ref = await ctx.put_binary(wav, "audio/wav", file_name)

        result: dict[str, Any] = {
            "duration_seconds": duration,
            "marks": marks,
            "audio": {
                "binary_property": binary_property,
                "encoding": "LINEAR16",
                "mime_type": "audio/wav",
                "sample_rate_hertz": 24_000,
                "size_bytes": len(wav),
                "file_name": file_name,
            },
            "voice": {"name": voice},
            "captions": captions,
            "usage": {
                "calls": len(calls),
                "tokens_prompt": tokens_prompt,
                "tokens_completion": tokens_completion,
            },
            "priced": total_cost is not None,
            "cost_usd": str(total_cost) if total_cost is not None else "",
            "input_mode": mode,
        }
        return Item.model_validate(
            {
                "json": {**item.json_, **result},
                "binary": {**(item.binary or {}), binary_property: ref},
            }
        )

    async def _synthesize_text(
        self, ctx: ExecutionContext, item: Item, voice: str, headers: dict[str, str]
    ) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]], list[GenerationCost]]:
        text = _text(ctx.param("text", item=item))
        if not text.strip():
            raise NodeConfigurationError(
                "Text is empty, so there is nothing to synthesize. Map this step's text from "
                "the script step, or type it in."
            )
        pcm, call = await self._call(ctx, text, voice=voice, headers=headers)
        return pcm, [], [], [call]

    async def _synthesize_captions(
        self, ctx: ExecutionContext, item: Item, voice: str, headers: dict[str, str]
    ) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]], list[GenerationCost]]:
        phrases: list[SsmlPhrase] = parse_phrases(_caption_list(ctx.param("captions", item=item)))
        gap_seconds = max(
            0.0,
            _number(
                ctx.param("phrase_gap_seconds", item=item),
                name="phrase_gap_seconds",
                default=_DEFAULT_PHRASE_GAP_SECONDS,
            ),
        )

        chunks: list[bytes] = []
        marks: list[dict[str, Any]] = []
        captions: list[dict[str, Any]] = []
        calls: list[GenerationCost] = []
        cumulative = 0.0

        for index, phrase in enumerate(phrases):
            pcm, call = await self._call(ctx, phrase.text, voice=voice, headers=headers)
            calls.append(call)
            phrase_seconds = pcm_duration_seconds(pcm)
            start = cumulative
            end = start + phrase_seconds
            marks.append({"name": phrase.name, "time_seconds": start})
            captions.append(
                {
                    "name": phrase.name,
                    "text": phrase.text,
                    "start_seconds": start,
                    "end_seconds": end,
                }
            )
            chunks.append(pcm)
            cumulative = end
            if gap_seconds > 0 and index < len(phrases) - 1:
                chunks.append(silence(gap_seconds))
                cumulative += gap_seconds

        return b"".join(chunks), marks, captions, calls

    async def _call(
        self, ctx: ExecutionContext, text: str, *, voice: str, headers: dict[str, str]
    ) -> tuple[bytes, GenerationCost]:
        pcm, generation_id = await _speak(ctx, text, voice=voice, headers=headers)
        cost = await generation_cost(ctx, generation_id, headers=headers)
        call = (
            cost
            if cost is not None
            else GenerationCost(tokens_prompt=0, tokens_completion=0, total_cost_usd=None)
        )
        ctx.report_usage(
            tokens_in=call.tokens_prompt,
            tokens_out=call.tokens_completion,
            provider="openrouter",
            model=MODEL_ID,
            cost_usd=call.total_cost_usd,
        )
        return pcm, call


def _caption_list(value: Any) -> list[Any]:
    """The phrase list, however the editor handed it over — same rule as
    `google_tts._caption_list`: a `json` param arrives parsed or as a string
    depending on whether an expression produced it."""
    import json

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise NodeConfigurationError(
                f"Phrases is not valid JSON (line {error.lineno}, column {error.colno}). It "
                'should be a list, like ["First line.", "Second line."].'
            ) from None
    if value is None:
        return []
    if not isinstance(value, list):
        raise NodeConfigurationError(
            f"Phrases must be a list and this is a {type(value).__name__}. Use the plain-text "
            "input for one block of narration."
        )
    return value


async def _speak(
    ctx: ExecutionContext, text: str, *, voice: str, headers: dict[str, str]
) -> tuple[bytes, str]:
    """One `/audio/speech` call: the audio, and the generation id to look its
    cost up by."""
    request_headers = {**headers, "Content-Type": "application/json"}
    try:
        response = await ctx.http().post(
            SPEECH_URL,
            json={
                "model": MODEL_ID,
                "input": text,
                "voice": voice,
                "response_format": "pcm",
            },
            headers=request_headers,
        )
    except httpx.HTTPError as error:
        raise OpenRouterUnavailable(
            f"Could not reach OpenRouter ({type(error).__name__}) — this is worth another attempt."
        ) from error

    raise_for_response(response, action="speech synthesis")
    pcm = response.content
    if not pcm:
        raise OpenRouterError(
            "OpenRouter answered with no audio content. Retrying an identical request will "
            "not help."
        )
    generation_id = response.headers.get("X-Generation-Id", "")
    return pcm, generation_id
