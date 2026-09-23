"""`shortvideo.shot_list` — the last free step before the money (V4.1).

A script model writes the short as JSON; everything after this node costs
something — a narration call, then one paid MiniMax generation per beat. So
this is where a script is held to the shape the rest of the pipeline assumes,
and **a bad one fails here, naming the beat, before any provider is called**.
An empty list, a list longer than the run is allowed to buy, a beat with no
visual prompt, a line too long to caption — each is a refusal with a sentence
an author can act on, not a MiniMax 4xx three nodes later.

**Why a node rather than a schema on the model step.** Tamtree's
`tamtree.agent_step` and `tamtree.llm_chat` return text, and `tamtree.extract`
pulls fields *out of* text rather than writing it. A model asked for JSON
mostly returns JSON — sometimes fenced, sometimes with a sentence before it.
Parsing leniently and validating strictly is the combination that survives
real model output without letting a malformed plan through.

**What it decides for the rest of the pipeline.** Beats are numbered here, 1
to N, and the number is the beat's identity from then on: the loop's
`on_item_error: skip` drops failed beats and shifts every later position, so
nothing downstream may identify a beat by index (see `beats-to-clips.yaml`).
Each beat's narration becomes one phrase whose SSML mark is named
`beat-<N>`, which is how `shortvideo.assemble` finds where each beat starts in
the narration without guessing.

**One caption per beat, so the narration line is capped at a caption.** A
beat is one generated clip — 6 s by default, 10 s at most on H3 — and a line
longer than ~90 characters does not fit in 6 s of speech anyway. Capping it at
the caption ceiling means the assembled timeline never has to split a line
across captions, and a line that would outrun its clip fails here rather than
as a frozen last frame in the draft.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Final

from tamtree_plugin_sdk import (
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
)

from tamtree_shortvideo.google_tts import MAX_INPUT_BYTES
from tamtree_shortvideo.minimax import MAX_PROMPT_CHARACTERS
from tamtree_shortvideo.ssml import build_ssml
from tamtree_shortvideo.timeline import MAX_CAPTION_CHARACTERS

__all__ = [
    "BEAT_MARK_PREFIX",
    "HARD_MAX_BEATS",
    "NODE_NAME",
    "ShotListNode",
    "beat_mark",
    "parse_script",
]

NODE_NAME: Final = "shortvideo.shot_list"

#: The Loop in `short-form-video.yaml` is capped at 25 passes with
#: `on_max_iterations: fail`. A plan longer than that would be refused there
#: after the narration was already paid for, so it is refused here instead.
HARD_MAX_BEATS: Final = 25
_DEFAULT_MAX_BEATS: Final = 8

#: The mark name each beat's narration phrase carries. `assemble` reads it
#: back off the narration's captions.
BEAT_MARK_PREFIX: Final = "beat-"

#: A fenced block anywhere in the reply — ```json … ``` or bare ``` … ```.
_FENCE: Final = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def beat_mark(beat_number: int) -> str:
    return f"{BEAT_MARK_PREFIX}{beat_number}"


def _decode(raw: Any) -> Any:
    """The script as data, from whatever form it arrived in.

    A string is tried as JSON, then as the contents of a fenced block, then as
    the outermost `{…}` or `[…]` span — the three shapes a model's "reply with
    JSON only" actually comes back in. Anything else is not a script.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in _FENCE.finditer(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise NodeConfigurationError(
        "The script is not JSON, so there is no shot list to read. Ask the script step to "
        'reply with only {"beats": [{"narration": "…", "visual_prompt": "…"}, …]}. '
        f"It began: {text[:120]!r}"
    )


def _text_field(entry: dict[str, Any], *names: str) -> str:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str):
            return value.strip()
    return ""


def parse_script(
    raw: Any, *, max_beats: int, max_narration_characters: int
) -> list[dict[str, Any]]:
    """Every beat, numbered and checked — or the first reason there are none.

    Accepts `{"beats": [...]}` or a bare list. A beat is an object with a
    narration line (`narration`, or `narration_text`) and a `visual_prompt`
    (or `visual`). Unknown fields are ignored rather than refused: a model
    adding a `title` is not a defect worth failing a run over.
    """
    data = _decode(raw)
    if isinstance(data, dict):
        data = data.get("beats")
    if not isinstance(data, list):
        raise NodeConfigurationError(
            'The script has no beat list. Expected {"beats": [...]} or a list of beats.'
        )
    if not data:
        raise NodeConfigurationError(
            "The script has no beats, so there is nothing to narrate or film."
        )
    if len(data) > max_beats:
        raise NodeConfigurationError(
            f"The script has {len(data)} beats and this step allows at most {max_beats}. "
            "Each beat is a paid video generation — ask for fewer, or raise the limit "
            f"(up to {HARD_MAX_BEATS})."
        )

    beats: list[dict[str, Any]] = []
    for position, entry in enumerate(data):
        number = position + 1
        where = f"Beat {number}"
        if not isinstance(entry, dict):
            raise NodeConfigurationError(
                f"{where} is {type(entry).__name__}, not an object with a narration line and "
                "a visual prompt."
            )
        narration = _text_field(entry, "narration", "narration_text")
        visual = _text_field(entry, "visual_prompt", "visual")
        if not narration:
            raise NodeConfigurationError(f"{where} has no narration line.")
        if not visual:
            raise NodeConfigurationError(
                f"{where} has no visual prompt, so there is nothing to generate footage from."
            )
        if len(narration) > max_narration_characters:
            raise NodeConfigurationError(
                f"{where}'s narration is {len(narration)} characters; the limit is "
                f"{max_narration_characters}. One beat is one clip and one caption — split "
                f"the line into two beats. It reads: {narration[:60]!r}…"
            )
        if "\n" in narration:
            narration = " ".join(narration.split())
        if len(visual) > MAX_PROMPT_CHARACTERS:
            raise NodeConfigurationError(
                f"{where}'s visual prompt is {len(visual):,} characters; MiniMax accepts at most "
                f"{MAX_PROMPT_CHARACTERS:,}."
            )
        beats.append({"beat_number": number, "narration_text": narration, "visual_prompt": visual})
    return beats


def _bounded_int(value: Any, *, name: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise NodeConfigurationError(f"{name!r} must be a whole number, not {value!r}.") from None
    if not low <= number <= high:
        raise NodeConfigurationError(f"{name!r} must be between {low} and {high}, not {number}.")
    return number


class ShotListNode(ProgrammaticNode):
    """Validate a script into numbered beats and the narration phrase list."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — shot list",
            "description": (
                "Check a script before anything is paid for: numbers each beat, refuses an "
                "empty, oversized or malformed plan by name, and builds the narration's "
                "phrase list with one timing mark per beat."
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
                            "beats": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "beat_number": {"type": "number"},
                                        "narration_text": {"type": "string"},
                                        "visual_prompt": {"type": "string"},
                                    },
                                    "required": [
                                        "beat_number",
                                        "narration_text",
                                        "visual_prompt",
                                    ],
                                },
                            },
                            "phrases": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                    "required": ["text", "name"],
                                },
                            },
                            "beat_count": {"type": "number"},
                            "narration_characters": {"type": "number"},
                        },
                        "required": ["beats", "phrases", "beat_count"],
                    },
                }
            ],
            "params": [
                {
                    "name": "script",
                    "label": "Script",
                    "type": "json",
                    "default": "",
                    "description": (
                        'The script as {"beats": [{"narration": "…", "visual_prompt": "…"}]}, '
                        "or as a model's reply containing it. Leave blank to read the incoming "
                        "item's `text` — what a Chat step returns."
                    ),
                },
                {
                    "name": "max_beats",
                    "label": "Most beats",
                    "type": "number",
                    "default": _DEFAULT_MAX_BEATS,
                    "description": (
                        "Refuse a script with more beats than this. Each beat is one paid "
                        f"video generation. At most {HARD_MAX_BEATS}."
                    ),
                },
                {
                    "name": "max_narration_characters",
                    "label": "Longest line",
                    "type": "number",
                    "default": MAX_CAPTION_CHARACTERS,
                    "description": (
                        "The most characters one beat's narration may have. It becomes one "
                        f"caption, so at most {MAX_CAPTION_CHARACTERS}; lower it for shorter clips."
                    ),
                },
            ],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        items = ctx.input_items() or [Item()]
        out: list[Item] = []
        for item in items:
            raw = ctx.param("script", item=item)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raw = (item.json_ or {}).get("text")
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raise NodeConfigurationError(
                    "The shot list has no script to read: the Script parameter is blank and "
                    "the incoming item has no `text`."
                )
            max_beats = _bounded_int(
                ctx.param("max_beats", item=item),
                name="max_beats",
                default=_DEFAULT_MAX_BEATS,
                low=1,
                high=HARD_MAX_BEATS,
            )
            max_characters = _bounded_int(
                ctx.param("max_narration_characters", item=item),
                name="max_narration_characters",
                default=MAX_CAPTION_CHARACTERS,
                low=10,
                high=MAX_CAPTION_CHARACTERS,
            )
            beats = parse_script(raw, max_beats=max_beats, max_narration_characters=max_characters)
            phrases = [
                {"text": beat["narration_text"], "name": beat_mark(beat["beat_number"])}
                for beat in beats
            ]
            # The narration step would refuse an oversized document too — but
            # after the run had already started paying. `build_ssml` is the
            # same function it calls, so the two cannot disagree on the size.
            build_ssml(phrases, max_bytes=MAX_INPUT_BYTES)
            out.append(
                Item.model_validate(
                    {
                        "json": {
                            "beats": beats,
                            "phrases": phrases,
                            "beat_count": len(beats),
                            "narration_characters": sum(
                                len(beat["narration_text"]) for beat in beats
                            ),
                        }
                    }
                )
            )
        return {"main": out}
