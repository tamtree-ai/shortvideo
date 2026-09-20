"""`shortvideo.google_tts` — narration, and the timing the rest of the short hangs on.

This is the first node in §3's pipeline and the one that fixes the timeline:
everything downstream trims, splits and captions against the duration and the
marks measured here (§3, "audio drives the timeline, not the video").

**Why v1beta1 and not v1.** Caption timing comes from `<mark>` timepoints
(D6), and `enableTimePointing` exists only on the v1beta1 `text:synthesize`
contract. That is the whole reason for the version, and it is worth knowing
that the field — not the audio — is what pins it.

**Why a price parameter rather than a built-in rate table.** §7 is explicit:
"No vendor price is copied into runtime defaults. Rates and terms age." But
§5.2's decision is equally explicit that every paid node must report a real
`cost_usd`. The only way to honour both is to make the rate an input the
operator fills in from Google's own pricing page — and to refuse the call when
it is missing, *before* any money is spent, rather than quietly synthesizing an
unpriced request.

**And an unpriced call is worse off than "counted as unpriced".** This node's
refusal used to be justified by `unpriced_block_count` catching what it let
through. It would not: a `report_usage` record with no tokens and no cost is
dropped before the ledger (`packages/engine/tamtree_engine/activities/
pipeline.py:891-894 @ d73c2d3e`), and the NULL-source row that results is then
excluded from `unpriced_calls` as well (`packages/server/tamtree_server/
cost_bands.py:48-52 @ d73c2d3e`). Nothing downstream would ever see the spend.
That makes the refusal *more* load-bearing, not less — it is the only guard
there is. `minimax_collect` reaches the same end by a different route, because
Google publishes a rate an operator can look up and MiniMax does not: there,
the rate is required on the credential and `0` is an answer somebody has to
choose.

**What the node refuses to guess.** Three things, each a named error: an input
over the documented 5,000-byte synchronous limit (truncating narration mid-
sentence is worse than failing); audio whose duration cannot be measured
exactly (`audio_duration`); and marks that were asked for and did not come
back, which is how a voice that does not support `<mark>` — Studio voices
among them — announces itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
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

from tamtree_shortvideo import google_auth
from tamtree_shortvideo.audio_duration import MEASURABLE_ENCODINGS, duration_seconds
from tamtree_shortvideo.credentials import CREDENTIAL_TYPE
from tamtree_shortvideo.ssml import SsmlDocument, build_ssml

__all__ = [
    "MAX_INPUT_BYTES",
    "NODE_NAME",
    "GoogleTtsNode",
    "SynthesisError",
    "SynthesisUnavailable",
    "TimepointsUnsupported",
    "mark_names",
]

NODE_NAME: Final = "shortvideo.google_tts"

#: v1beta1 for `enableTimePointing`; see the module docstring.
SYNTHESIZE_URL: Final = "https://texttospeech.googleapis.com/v1beta1/text:synthesize"

#: "Total bytes per request: 5,000" — the documented synchronous quota. Counted
#: in UTF-8 bytes, not characters, which is the distinction that matters for
#: the locales where one character is several bytes.
MAX_INPUT_BYTES: Final = 5_000

#: How far a timepoint may sit past the measured end before it is treated as
#: contradictory rather than as rounding. Google reports to the millisecond;
#: a frame at 30fps is 33ms.
_MARK_TOLERANCE_SECONDS: Final = 0.05

#: MIME type per encoding, for the attachment. `LINEAR16` arrives WAV-wrapped.
_MIME_TYPES: Final = {
    "LINEAR16": "audio/wav",
    "MP3": "audio/mpeg",
    "OGG_OPUS": "audio/ogg",
}

_FILE_SUFFIXES: Final = {"LINEAR16": "wav", "MP3": "mp3", "OGG_OPUS": "ogg"}

#: `<mark name="beat-3"/>` in either quoting style, attributes in any order.
_MARK_PATTERN: Final = re.compile(r"<mark\b[^>]*?\bname\s*=\s*([\"'])(.*?)\1", re.IGNORECASE)

_DETAIL_LIMIT: Final = 300


class SynthesisError(NodeConfigurationError):
    """Google refused the request, or answered something unusable.

    Non-retryable, and deliberately so: a 4xx from `text:synthesize` is the
    step's own configuration — an unknown voice, a malformed SSML document, a
    speaking rate out of range — and a second identical attempt buys nothing.
    """


class TimepointsUnsupported(NodeConfigurationError):
    """Marks were requested and Google returned none, or not all of them.

    This is D6's "default failure is actionable": support for `<mark>` varies
    by voice, so the node says which voice and which marks rather than handing
    the compositor a caption track with holes in it.
    """


class SynthesisUnavailable(RuntimeError):
    """The endpoint did not answer, or answered 5xx.

    Not a `NodeConfigurationError`: nothing is wrong with the request, so the
    engine's retry budget should have its go.
    """


def mark_names(ssml: str) -> list[str]:
    """Every `<mark name="…">` in document order, duplicates preserved.

    Duplicates are preserved rather than deduplicated because a repeated name
    is a defect the caller needs to see: Google returns one timepoint per mark
    *occurrence*, so two marks sharing a name produce a caption track that
    cannot be matched back to its phrases. `_check_marks` is where that is
    reported.
    """
    return [match.group(2) for match in _MARK_PATTERN.finditer(ssml)]


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


class GoogleTtsNode(ProgrammaticNode):
    """Synthesize one narration track per item, with its measured timing."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — Google Text-to-Speech",
            "description": (
                "Synthesize narration with Google Cloud Text-to-Speech. Returns the audio "
                "as an attachment, its measured duration, and caption timings from any "
                "SSML <mark> tags."
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
                                "properties": {
                                    "language_code": {"type": "string"},
                                    "name": {"type": "string"},
                                    "ssml_gender": {"type": "string"},
                                },
                                "required": ["language_code"],
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
                            "billed_characters": {"type": "number"},
                            "cost_usd": {"type": "string"},
                            "input_mode": {"type": "string"},
                        },
                        "required": [
                            "duration_seconds",
                            "marks",
                            "audio",
                            "voice",
                            "billed_characters",
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
                        {"value": "ssml", "label": "SSML — for caption marks and fine control"},
                        {
                            "value": "captions",
                            "label": "Phrase list — SSML and caption timings built for you",
                        },
                    ],
                    "description": (
                        "Plain text is read as written. SSML lets you place <mark> tags, "
                        "which come back as caption timings. A phrase list builds that SSML "
                        "for you — escaping, mark names and the size limit handled — and "
                        "returns each phrase with the time it is spoken."
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
                    "name": "ssml",
                    "label": "SSML",
                    "type": "string",
                    "default": "",
                    "description": (
                        "A complete SSML document, <speak> root included. Every <mark> in it "
                        "must come back as a timing or the step fails by name."
                    ),
                    "display_options": {"show": {"input_mode": ["ssml"]}},
                },
                {
                    "name": "captions",
                    "label": "Phrases",
                    "type": "json",
                    "default": [],
                    "description": (
                        'A list of phrases — ["First line.", "Second line."] — or objects '
                        'like {"text": "…", "name": "own-mark-name"}. One caption per entry, '
                        "spoken in order."
                    ),
                    "display_options": {"show": {"input_mode": ["captions"]}},
                },
                {
                    "name": "language_code",
                    "label": "Language",
                    "type": "string",
                    "default": "en-US",
                    "required": True,
                    "description": "BCP-47 tag, e.g. en-US, en-GB, de-DE.",
                },
                {
                    "name": "voice_name",
                    "label": "Voice",
                    "type": "string",
                    "default": "",
                    "description": (
                        "A specific voice, e.g. en-US-Neural2-C. Leave empty to let Google "
                        "pick one for the language and gender. Studio voices do not support "
                        "<mark>, so they cannot produce caption timings."
                    ),
                },
                {
                    "name": "ssml_gender",
                    "label": "Gender",
                    "type": "options",
                    "default": "SSML_VOICE_GENDER_UNSPECIFIED",
                    "options": [
                        {"value": "SSML_VOICE_GENDER_UNSPECIFIED", "label": "Any"},
                        {"value": "FEMALE", "label": "Female"},
                        {"value": "MALE", "label": "Male"},
                        {"value": "NEUTRAL", "label": "Neutral"},
                    ],
                    "description": "Ignored when a specific voice is named.",
                },
                {
                    "name": "audio_encoding",
                    "label": "Audio format",
                    "type": "options",
                    "default": "LINEAR16",
                    "options": [
                        {"value": "LINEAR16", "label": "WAV (LINEAR16) — lossless, for editing"},
                        {"value": "MP3", "label": "MP3 — 32kbps"},
                        {"value": "OGG_OPUS", "label": "Ogg Opus"},
                    ],
                    "description": (
                        "Only formats whose duration can be measured exactly are offered — "
                        "the timeline is built on that measurement."
                    ),
                },
                {
                    "name": "speaking_rate",
                    "label": "Speaking rate",
                    "type": "number",
                    "default": 1.0,
                    "description": "0.25 to 2.0. 1.0 is the voice's normal speed.",
                },
                {
                    "name": "pitch",
                    "label": "Pitch",
                    "type": "number",
                    "default": 0,
                    "description": "Semitones, -20.0 to 20.0.",
                },
                {
                    "name": "volume_gain_db",
                    "label": "Volume gain (dB)",
                    "type": "number",
                    "default": 0,
                    "description": (
                        "-96.0 to 16.0. Leave at 0 — the compositor loudness-normalises "
                        "narration later, and gain applied twice fights itself."
                    ),
                },
                {
                    "name": "sample_rate_hertz",
                    "label": "Sample rate (Hz)",
                    "type": "number",
                    "default": 0,
                    "description": "0 uses the voice's native rate, which is usually right.",
                },
                {
                    "name": "output_binary_property",
                    "label": "Attachment name",
                    "type": "string",
                    "default": "audio",
                    "description": "Which binary property on the output item holds the audio.",
                },
                {
                    "name": "price_usd_per_million_chars",
                    "label": "Price per 1M characters (USD)",
                    "type": "number",
                    "default": 0,
                    "required": True,
                    "description": (
                        "The rate for the voice tier you are using, from "
                        "https://cloud.google.com/text-to-speech/pricing. Required: an "
                        "unpriced paid call reaches the workspace budget as nothing at all "
                        "— not even as an unpriced call — so this step refuses to run "
                        "without it. No rate is built in, because vendor prices age and a "
                        "stale default would under-report real spend."
                    ),
                },
                {
                    "name": "require_timepoints",
                    "label": "Fail if caption marks are missing",
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "On by default: a voice that silently drops <mark> tags produces a "
                        "short with no captions and no warning. Turn off only when the marks "
                        "are genuinely optional."
                    ),
                },
            ],
            "credentials": [{"type": CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        token = await google_auth.access_token(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._synthesize(ctx, item, token))
        return {"main": outputs}

    async def _synthesize(self, ctx: ExecutionContext, item: Item, token: str) -> Item:
        mode = (_text(ctx.param("input_mode", item=item)) or "text").lower()
        body_text, document = _input(ctx, item, mode=mode)
        encoding = (_text(ctx.param("audio_encoding", item=item)) or "LINEAR16").upper()
        language_code = _text(ctx.param("language_code", item=item)) or "en-US"
        voice_name = _text(ctx.param("voice_name", item=item)).strip()
        gender = _text(ctx.param("ssml_gender", item=item)) or "SSML_VOICE_GENDER_UNSPECIFIED"
        binary_property = _text(ctx.param("output_binary_property", item=item)) or "audio"
        require_marks = ctx.param("require_timepoints", item=item)
        require_marks = True if require_marks is None else bool(require_marks)

        # Everything that can be refused for free is refused before the call.
        _check_input(body_text, mode=mode)
        _check_encoding(encoding)
        rate_per_million = _price(ctx, item)
        expected_marks = [] if mode == "text" else mark_names(body_text)

        request: dict[str, Any] = {
            # A built document is SSML as far as the API is concerned; the
            # phrase list is this node's convenience, not Google's contract.
            "input": {"text" if mode == "text" else "ssml": body_text},
            "voice": _voice(language_code, voice_name, gender),
            "audioConfig": _audio_config(ctx, item, encoding),
        }
        if expected_marks:
            request["enableTimePointing"] = ["SSML_MARK"]

        payload = await _post(ctx, request, token=token)
        audio = _decode_audio(payload)
        seconds = duration_seconds(audio, encoding=encoding)
        marks = _marks(payload)
        _check_marks(
            marks,
            expected=expected_marks,
            duration=seconds,
            voice=voice_name or f"{language_code} ({gender})",
            require=require_marks,
        )

        # Billed on the characters sent, which is an upper bound: Google
        # excludes <mark> tags from the count, so a marked-up document is
        # charged slightly less than this reports. Over-reporting is the safe
        # direction for a budget that stops work when it is reached.
        billed = len(body_text)
        cost = (Decimal(billed) * rate_per_million / Decimal(1_000_000)).quantize(
            Decimal("0.00000001")
        )
        ctx.report_usage(
            # A synthesis call has no tokens. `report_usage` requires the two
            # counts anyway (§5.2), so they are zero and the figure rides in
            # `cost_usd` — the shape problem is a live ask on the tool-cost
            # subproject, not something this node can fix.
            tokens_in=0,
            tokens_out=0,
            provider="google",
            model=voice_name or language_code,
            cost_usd=cost,
        )

        suffix = _FILE_SUFFIXES[encoding]
        file_name = f"{ctx.node_id}.{suffix}"
        mime_type = _MIME_TYPES[encoding]
        ref = await ctx.put_binary(audio, mime_type, file_name)

        returned_config = payload.get("audioConfig")
        returned_config = returned_config if isinstance(returned_config, dict) else {}
        result: dict[str, Any] = {
            "duration_seconds": seconds,
            "marks": marks,
            "audio": {
                "binary_property": binary_property,
                "encoding": encoding,
                "mime_type": mime_type,
                "sample_rate_hertz": returned_config.get("sampleRateHertz"),
                "size_bytes": len(audio),
                "file_name": file_name,
            },
            "voice": {
                "language_code": language_code,
                "name": voice_name,
                "ssml_gender": gender,
            },
            "captions": _captions(document, marks, duration=seconds),
            "billed_characters": billed,
            "cost_usd": str(cost),
            "input_mode": mode,
        }
        return Item.model_validate(
            {
                # The source json is retained and the node's fields layered on
                # top, so a beat's `{narration, visual prompt}` still travels
                # with its audio into the loop that generates footage.
                "json": {**item.json_, **result},
                # §9's BinaryRef preservation: whatever the item already
                # carried stays, and the narration joins it.
                "binary": {**(item.binary or {}), binary_property: ref},
            }
        )


def _input(ctx: ExecutionContext, item: Item, *, mode: str) -> tuple[str, SsmlDocument | None]:
    """The document to send, and the phrase list behind it when there was one.

    The third mode is a *generator*, not a third API field: Google takes text
    or SSML, and a phrase list becomes SSML here. Keeping that translation in
    one place is what lets `captions` be returned with real timings later in
    the same pass.
    """
    if mode == "text":
        return _text(ctx.param("text", item=item)), None
    if mode == "ssml":
        return _text(ctx.param("ssml", item=item)), None
    if mode == "captions":
        document = build_ssml(
            _caption_list(ctx.param("captions", item=item)), max_bytes=MAX_INPUT_BYTES
        )
        return document.ssml, document
    raise NodeConfigurationError(
        f"Unknown input mode {mode!r} — choose 'text', 'ssml' or 'captions'."
    )


def _caption_list(value: Any) -> list[Any]:
    """The phrase list, however the editor handed it over.

    A `json` param arrives parsed when the author used the JSON editor and as
    a string when it came through an expression, and a flow should not fail
    over which of the two happened.
    """
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


def _captions(
    document: SsmlDocument | None, marks: list[dict[str, Any]], *, duration: float
) -> list[dict[str, Any]]:
    """Each phrase joined to the moment it is spoken, and to when it ends.

    D6's payoff: caption text and caption timing meet here, with no second
    model and no transcription step. A caption runs until the next one starts,
    and the last runs to the end of the audio — which is why the measured
    duration has to be exact and not estimated.

    Only phrases whose mark actually came back are included; a missing mark is
    already a named failure unless the author turned that check off, and in
    that case a caption with no timing is worse than no caption.
    """
    if document is None:
        return []
    timings = {mark["name"]: float(mark["time_seconds"]) for mark in marks}
    timed = [
        (phrase, timings[phrase.name]) for phrase in document.phrases if phrase.name in timings
    ]
    captions: list[dict[str, Any]] = []
    for index, (phrase, start) in enumerate(timed):
        end = timed[index + 1][1] if index + 1 < len(timed) else duration
        captions.append(
            {
                "name": phrase.name,
                "text": phrase.text,
                "start_seconds": start,
                "end_seconds": max(start, end),
            }
        )
    return captions


def _check_input(body: str, *, mode: str) -> None:
    if not body.strip():
        label = {"text": "Text", "ssml": "SSML"}.get(mode, "The narration")
        raise NodeConfigurationError(
            f"{label} is empty, so there is nothing to synthesize. Map this step's "
            f"{label.lower()} from the script step, or type it in."
        )
    size = len(body.encode("utf-8"))
    if size > MAX_INPUT_BYTES:
        raise NodeConfigurationError(
            f"This request is {size:,} UTF-8 bytes and Google's synchronous synthesize limit "
            f"is {MAX_INPUT_BYTES:,}. Split the narration into shorter beats — truncating it "
            "here would cut a sentence in half and the short would never mention it. Note "
            "that SSML tags count toward the limit, and in some languages one character is "
            "several bytes."
        )


def _check_encoding(encoding: str) -> None:
    if encoding not in MEASURABLE_ENCODINGS:
        raise NodeConfigurationError(
            f"Audio format {encoding!r} is not offered: the timeline is built on an exactly "
            f"measured duration, and only {', '.join(MEASURABLE_ENCODINGS)} carry one."
        )


def _price(ctx: ExecutionContext, item: Item) -> Decimal:
    """The operator's rate, as an exact `Decimal`, or a named refusal.

    `Decimal(str(...))` rather than `Decimal(float)`: 16.0 entered in the editor
    must cost $16.00 per million characters and not $15.999999999999998.
    """
    raw = ctx.param("price_usd_per_million_chars", item=item)
    value = _number(raw, name="price_usd_per_million_chars", default=0.0)
    if value <= 0:
        raise NodeConfigurationError(
            "Set 'Price per 1M characters (USD)' for the voice tier you are using — see "
            "https://cloud.google.com/text-to-speech/pricing. Text-to-Speech is billed per "
            "character, and a paid call reported without a cost never reaches the workspace "
            "budget at all — not even as an unpriced call its block count could catch. No "
            "rate is built in on purpose: a vendor price baked into a release goes stale and "
            "quietly under-reports what you are spending."
        )
    return Decimal(str(value))


def _voice(language_code: str, voice_name: str, gender: str) -> dict[str, Any]:
    voice: dict[str, Any] = {"languageCode": language_code}
    if voice_name:
        # `ssmlGender` is ignored by Google when a name is given; omitting it
        # keeps the request honest about what actually selected the voice.
        voice["name"] = voice_name
    elif gender and gender != "SSML_VOICE_GENDER_UNSPECIFIED":
        voice["ssmlGender"] = gender
    return voice


def _audio_config(ctx: ExecutionContext, item: Item, encoding: str) -> dict[str, Any]:
    config: dict[str, Any] = {"audioEncoding": encoding}
    speaking_rate = _number(
        ctx.param("speaking_rate", item=item), name="speaking_rate", default=1.0
    )
    pitch = _number(ctx.param("pitch", item=item), name="pitch", default=0.0)
    volume = _number(ctx.param("volume_gain_db", item=item), name="volume_gain_db", default=0.0)
    sample_rate = _number(
        ctx.param("sample_rate_hertz", item=item), name="sample_rate_hertz", default=0.0
    )

    # Sent only when they differ from the documented default, so the request
    # says what the step actually asked for and Google applies its own default
    # to everything else.
    if speaking_rate != 1.0:
        config["speakingRate"] = speaking_rate
    if pitch:
        config["pitch"] = pitch
    if volume:
        config["volumeGainDb"] = volume
    if sample_rate > 0:
        config["sampleRateHertz"] = int(sample_rate)
    return config


async def _post(ctx: ExecutionContext, request: dict[str, Any], *, token: str) -> dict[str, Any]:
    """One synthesize call, with every failure mode named.

    Through `ctx.http()` and never raw httpx (§19.4) — the SSRF policy lives on
    that seam.
    """
    try:
        response = await ctx.http().post(
            SYNTHESIZE_URL,
            json=request,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    except httpx.HTTPError as error:
        raise SynthesisUnavailable(
            f"Could not reach Google Text-to-Speech ({type(error).__name__}) — "
            "this is worth another attempt."
        ) from error

    if response.status_code == 429 or response.status_code >= 500:
        # 429 is a quota, not a mistake in the request: it clears on its own,
        # so it belongs on the retryable side of the split.
        raise SynthesisUnavailable(
            f"Google Text-to-Speech answered {response.status_code} — "
            "this is worth another attempt."
        )
    if response.status_code >= 400:
        raise SynthesisError(
            f"Google Text-to-Speech refused this request ({response.status_code}): "
            f"{_detail(response)}. Check the voice name, the language code, the SSML and the "
            "audio settings on this step; retrying an identical request will not help."
        )

    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SynthesisError(
            "Google Text-to-Speech answered with something that is not a JSON object. "
            "Retrying an identical request will not help."
        )
    return body


def _detail(response: httpx.Response) -> str:
    """Google's own words about the refusal, bounded.

    The error body is `{"error": {"message": …, "status": …}}` and carries
    neither the credential nor the synthesized audio.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:_DETAIL_LIMIT] or f"HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        status = str(error.get("status") or "").strip()
        detail = " — ".join(part for part in (status, message) if part)
    else:
        detail = str(error or "").strip()
    return detail[:_DETAIL_LIMIT] or f"HTTP {response.status_code}"


def _decode_audio(payload: dict[str, Any]) -> bytes:
    content = payload.get("audioContent")
    if not isinstance(content, str) or not content:
        raise SynthesisError(
            "Google Text-to-Speech answered without any audio content. Retrying an identical "
            "request will not help; check the voice supports the requested audio format."
        )
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise SynthesisError(
            "Google Text-to-Speech returned audio that is not valid base64, so it cannot be "
            "decoded. Retrying an identical request will not help."
        ) from None


def _marks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`timepoints[{markName,timeSeconds}]` normalized to `{name,time_seconds}`.

    Normalized here rather than passed through so the rest of the pipeline —
    and any second TTS provider — speaks one caption vocabulary (D9).
    """
    raw = payload.get("timepoints")
    if not isinstance(raw, list):
        return []
    marks: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("markName")
        seconds = entry.get("timeSeconds", 0.0)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 0.0
        marks.append({"name": "" if name is None else str(name), "time_seconds": seconds})
    return marks


def _check_marks(
    marks: list[dict[str, Any]],
    *,
    expected: list[str],
    duration: float,
    voice: str,
    require: bool,
) -> None:
    """Every invariant the compositor will assume, checked where it can be named.

    A caption track that is short a mark, out of order, or pointing past the
    end of the audio is not a smaller problem than a failed step — it is the
    same problem discovered after the render.
    """
    if expected:
        duplicates = sorted({name for name in expected if expected.count(name) > 1})
        if duplicates:
            raise TimepointsUnsupported(
                f"The SSML reuses the mark name(s) {', '.join(duplicates)}. Google returns one "
                "timing per <mark>, so repeated names cannot be matched back to their phrases. "
                "Give every mark a distinct name."
            )
        returned = [mark["name"] for mark in marks]
        missing = [name for name in expected if name not in returned]
        if missing and require:
            raise TimepointsUnsupported(
                f"The voice {voice!r} returned no timing for {len(missing)} of "
                f"{len(expected)} caption marks ({', '.join(missing[:5])}"
                f"{', …' if len(missing) > 5 else ''}). Support for <mark> varies by voice — "
                "Studio voices do not support it at all. Choose a Standard, WaveNet or Neural2 "
                "voice, or turn off 'Fail if caption marks are missing' if the captions are "
                "genuinely optional here."
            )

    previous = -1.0
    for mark in marks:
        seconds = float(mark["time_seconds"])
        if seconds < previous:
            raise TimepointsUnsupported(
                f"Caption mark {mark['name']!r} is timed at {seconds:.3f}s, before the mark "
                f"ahead of it at {previous:.3f}s. Captions built from this would run backwards; "
                "this is a provider fault worth reporting rather than retrying."
            )
        if seconds > duration + _MARK_TOLERANCE_SECONDS:
            raise TimepointsUnsupported(
                f"Caption mark {mark['name']!r} is timed at {seconds:.3f}s but the audio is "
                f"only {duration:.3f}s long. A caption cannot be shown past the end of its "
                "narration; this is a provider fault worth reporting rather than retrying."
            )
        previous = seconds
