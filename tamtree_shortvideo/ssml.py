"""Turn a beat's phrases into SSML that Google will accept, and captions back.

D6 in one module: "if you emit one mark per phrase in the SSML you already
generate, you get caption timing for free and exactly aligned, with no second
model in the path." This is the generator that makes that true — and the place
every way it can go wrong is handled once rather than in every flow that
builds a `<speak>` document by hand in an expression field.

**What hand-built SSML gets wrong.** A script step writes "Marks & Spencer's
Q3 < Q4" into a template and the request becomes malformed XML; Google answers
`INVALID_ARGUMENT` and the author is left staring at a working sentence. Or
two phrases are given the same mark name, and the caption track quietly
mismatches its text. Or the document creeps past 5,000 bytes and the last
phrase of the short is never spoken. Escaping, naming and measuring are not
niceties here; they are the three ways this step fails in production.

**Why it fails rather than trims.** An over-long beat is a *script* problem
with a real fix — split the beat, which §3's pipeline is built to do anyway.
Truncating to fit would produce a short that is fluent, complete-sounding and
missing its conclusion, which is the worst of the available outcomes. So
`build_ssml` refuses, and `fit_phrases` says how many phrases would have fit,
so the refusal can name the split instead of just the limit.

**Streaming is not an option here.** The streaming synthesize contract does
not carry this SSML path, so the pipeline cannot trade the byte limit for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from tamtree_plugin_sdk import NodeConfigurationError

__all__ = [
    "MAX_MARK_NAME_LENGTH",
    "SsmlDocument",
    "SsmlPhrase",
    "build_ssml",
    "escape_text",
    "fit_phrases",
]

#: Escaped in text content. `>` does not strictly require it, but escaping it
#: means no sequence of user text can ever close a construct — `]]>` included.
_ESCAPES: Final = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

#: XML 1.0 forbids the C0 controls outright, tab/newline/carriage return
#: excepted. They cannot be escaped into validity, so they are refused.
_FORBIDDEN_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: What survives into a mark name. Google echoes the name back verbatim, and
#: the name also lives in an XML attribute, so it is kept to the character set
#: that is unambiguous in both.
_UNSAFE_IN_NAME: Final = re.compile(r"[^A-Za-z0-9_.-]+")

MAX_MARK_NAME_LENGTH: Final = 64


@dataclass(frozen=True)
class SsmlPhrase:
    """One caption: the mark that will time it, and the text it will show."""

    name: str
    text: str


@dataclass(frozen=True)
class SsmlDocument:
    """A complete `<speak>` document and the phrases whose marks are in it."""

    ssml: str
    phrases: tuple[SsmlPhrase, ...]

    @property
    def size_bytes(self) -> int:
        return len(self.ssml.encode("utf-8"))

    @property
    def mark_names(self) -> list[str]:
        return [phrase.name for phrase in self.phrases]


def escape_text(text: str) -> str:
    """User text, safe to drop between tags.

    Ampersand first: escaping it after `<` would turn the `&lt;` just written
    into `&amp;lt;`.
    """
    control = _FORBIDDEN_CONTROLS.search(text)
    if control is not None:
        raise NodeConfigurationError(
            f"The narration contains a control character (0x{ord(control.group()):02x}) that "
            "XML cannot represent, so it cannot be sent as SSML. It is almost always a stray "
            "byte from a copy-paste — retype the phrase, or strip control characters in the "
            "step before this one."
        )
    for character, replacement in _ESCAPES.items():
        text = text.replace(character, replacement)
    return text


def _phrase_text(entry: Any, *, index: int) -> tuple[str, str | None]:
    """`(text, requested name)` from either shape a caption list can take.

    A plain list of strings is what a script step naturally produces; the
    object form exists for a caller that wants to choose its own mark names —
    matching them to ids it already has, say.
    """
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, Mapping):
        text = entry.get("text")
        if not isinstance(text, str):
            raise NodeConfigurationError(
                f"Caption {index + 1} is an object with no 'text' — each entry must be either "
                'a string or an object like {"text": "…", "name": "optional-mark-name"}.'
            )
        name = entry.get("name")
        return text, name if isinstance(name, str) and name.strip() else None
    raise NodeConfigurationError(
        f"Caption {index + 1} is a {type(entry).__name__}, not text. Each entry must be either "
        'a string or an object like {"text": "…", "name": "optional-mark-name"}.'
    )


def _mark_name(requested: str | None, *, index: int, prefix: str, taken: set[str]) -> str:
    """A name that is valid in XML, echoed back intact by Google, and unique.

    Uniqueness is enforced here rather than reported, because the alternative
    is a caption track whose timings cannot be matched to their text — and the
    caller who supplied two identical ids usually has no way to fix them.
    """
    base = _UNSAFE_IN_NAME.sub("-", requested).strip("-") if requested else ""
    if not base or not (base[0].isalpha() or base[0] == "_"):
        base = f"{prefix}{index}" if not base else f"{prefix}-{base}"
    base = base[:MAX_MARK_NAME_LENGTH]

    name = base
    suffix = 2
    while name in taken:
        tail = f"-{suffix}"
        name = base[: MAX_MARK_NAME_LENGTH - len(tail)] + tail
        suffix += 1
    taken.add(name)
    return name


def build_ssml(
    captions: Sequence[Any],
    *,
    mark_prefix: str = "p",
    max_bytes: int | None = None,
) -> SsmlDocument:
    """One `<speak>` document, one `<mark>` per phrase, in order.

    `max_bytes` is checked against the finished document when given — the only
    size that matters, since the tags count toward Google's limit too.
    """
    if not isinstance(captions, Sequence) or isinstance(captions, (str, bytes)):
        raise NodeConfigurationError(
            "Captions must be a list of phrases. A single string is not a list — if you have "
            "one block of narration, use the plain-text input instead."
        )
    if not captions:
        raise NodeConfigurationError(
            "The caption list is empty, so there is nothing to narrate. Map it from the "
            "script step's shot list."
        )

    taken: set[str] = set()
    phrases: list[SsmlPhrase] = []
    for index, entry in enumerate(captions):
        text, requested = _phrase_text(entry, index=index)
        if not text.strip():
            raise NodeConfigurationError(
                f"Caption {index + 1} is empty. An empty phrase produces a mark with no words "
                "under it, which is a caption that flashes and says nothing — remove it, or "
                "give it text."
            )
        phrases.append(
            SsmlPhrase(
                name=_mark_name(requested, index=index, prefix=mark_prefix, taken=taken),
                text=" ".join(text.split()),
            )
        )

    document = SsmlDocument(ssml=_render(phrases), phrases=tuple(phrases))
    if max_bytes is not None and document.size_bytes > max_bytes:
        fitting = fit_phrases(captions, max_bytes=max_bytes, mark_prefix=mark_prefix)
        raise NodeConfigurationError(
            f"These {len(phrases)} captions build a {document.size_bytes:,}-byte SSML document "
            f"and Google's synchronous synthesize limit is {max_bytes:,} bytes. "
            f"About the first {fitting} would fit in one request — split this beat there rather "
            "than shortening the script, so nothing goes unsaid. Note that the SSML tags count "
            "toward the limit, and in some languages one character is several bytes."
        )
    return document


def _render(phrases: Sequence[SsmlPhrase]) -> str:
    """The document itself. Marks lead their phrase, so a timepoint is the
    moment that caption should appear."""
    body = " ".join(f'<mark name="{phrase.name}"/>{escape_text(phrase.text)}' for phrase in phrases)
    return f"<speak>{body}</speak>"


def fit_phrases(captions: Sequence[Any], *, max_bytes: int, mark_prefix: str = "p") -> int:
    """How many leading captions build to a document within `max_bytes`.

    What makes an over-long beat's refusal actionable: "split after the
    seventh" is a instruction, "5,000 bytes" is a number.
    """
    fitting = 0
    for count in range(1, len(captions) + 1):
        try:
            document = build_ssml(captions[:count], mark_prefix=mark_prefix)
        except NodeConfigurationError:
            break
        if document.size_bytes > max_bytes:
            break
        fitting = count
    return fitting
