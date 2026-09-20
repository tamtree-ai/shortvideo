"""The SSML generator — escaping, mark naming, and the size limit.

Three failure modes that all look like working code until a real script hits
them: an apostrophe-and-ampersand sentence that makes the request malformed, a
repeated mark name that misaligns the caption track, and a beat that creeps
past 5,000 bytes and loses its last sentence. Each gets its own group below.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree

import pytest
from tamtree_plugin_sdk import NodeConfigurationError

from tamtree_shortvideo.google_tts import MAX_INPUT_BYTES, mark_names
from tamtree_shortvideo.ssml import (
    MAX_MARK_NAME_LENGTH,
    build_ssml,
    escape_text,
    fit_phrases,
    parse_phrases,
)


def _parsed(ssml: str) -> ElementTree.Element:
    """Parsed as XML, which is the only definition of "valid" that matters
    here — Google's parser is not going to be more forgiving than this one."""
    return ElementTree.fromstring(ssml)


# -- the shape of the document -----------------------------------------------


def test_one_mark_leads_each_phrase_in_order() -> None:
    document = build_ssml(["First line.", "Second line.", "Third line."])

    assert document.ssml == (
        '<speak><mark name="p0"/>First line. '
        '<mark name="p1"/>Second line. '
        '<mark name="p2"/>Third line.</speak>'
    )
    assert document.mark_names == ["p0", "p1", "p2"]


def test_the_document_the_node_sends_is_the_document_the_node_reads_back() -> None:
    """`build_ssml` names the marks and `mark_names` finds them again. If those
    two ever disagreed, every phrase list would fail as an unsupported voice."""
    document = build_ssml(["One.", "Two.", "Three."])

    assert mark_names(document.ssml) == document.mark_names


def test_each_phrase_keeps_its_text_for_the_caption_track() -> None:
    document = build_ssml([{"text": "Compound interest.", "name": "hook"}])

    assert document.phrases[0].text == "Compound interest."
    assert document.phrases[0].name == "hook"


def test_whitespace_is_collapsed_so_a_caption_is_one_line() -> None:
    """A script step's output is usually wrapped. A caption rendered with the
    line breaks still in it lays out wrongly over the video."""
    document = build_ssml(["First\n  line\twraps."])

    assert document.phrases[0].text == "First line wraps."


# -- escaping ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("Marks & Spencer", "Marks &amp; Spencer"),
        ("Q3 < Q4", "Q3 &lt; Q4"),
        ("Q4 > Q3", "Q4 &gt; Q3"),
        ("a ]]> b", "a ]]&gt; b"),
        ("5 < 6 & 7 > 6", "5 &lt; 6 &amp; 7 &gt; 6"),
    ],
)
def test_user_text_cannot_break_out_of_the_document(raw: str, escaped: str) -> None:
    assert escape_text(raw) == escaped


def test_the_ampersand_is_escaped_before_the_angle_brackets() -> None:
    """Escaping `<` first would turn the `&lt;` just written into `&amp;lt;`,
    and the narration would literally say "ampersand ell tee"."""
    assert escape_text("<") == "&lt;"
    assert escape_text("&lt;") == "&amp;lt;"


def test_a_sentence_full_of_markup_still_parses() -> None:
    document = build_ssml(["Marks & Spencer's Q3 < Q4 <b>really</b>."])

    (text,) = [element.tail for element in _parsed(document.ssml)]
    assert text == "Marks & Spencer's Q3 < Q4 <b>really</b>."


def test_a_control_character_is_refused_rather_than_stripped() -> None:
    """It cannot be escaped into validity, and silently dropping a byte from
    someone's script is not this module's call to make."""
    with pytest.raises(NodeConfigurationError, match="control character"):
        build_ssml(["A stray \x07 bell."])


@pytest.mark.parametrize("whitespace", ["\t", "\n", "\r"])
def test_ordinary_whitespace_is_not_treated_as_a_control_character(
    whitespace: str,
) -> None:
    assert build_ssml([f"Two{whitespace}words."]).phrases[0].text == "Two words."


# -- mark names --------------------------------------------------------------


def test_repeated_names_are_made_unique_rather_than_reported() -> None:
    """Google returns one timing per mark *occurrence*. Two marks called `beat`
    produce a caption track that cannot be matched back to its phrases, and the
    caller who supplied the duplicate ids usually cannot fix them."""
    document = build_ssml(
        [
            {"text": "One.", "name": "beat"},
            {"text": "Two.", "name": "beat"},
            {"text": "Three.", "name": "beat"},
        ]
    )

    assert document.mark_names == ["beat", "beat-2", "beat-3"]


def test_a_name_that_is_not_valid_in_xml_is_made_valid() -> None:
    document = build_ssml([{"text": "One.", "name": "beat #1 (intro)"}])

    (name,) = document.mark_names
    assert name == "beat-1-intro"
    _parsed(document.ssml)  # the attribute really is well-formed


def test_a_name_that_cannot_start_an_identifier_is_prefixed() -> None:
    """XML names may not start with a digit, and a mark name that Google
    echoes back has to survive the round trip as written."""
    document = build_ssml([{"text": "One.", "name": "3rd beat"}])

    assert document.mark_names == ["p-3rd-beat"]


def test_a_very_long_name_is_bounded_and_still_unique() -> None:
    long_name = "a" * 200
    document = build_ssml(
        [{"text": "One.", "name": long_name}, {"text": "Two.", "name": long_name}]
    )

    first, second = document.mark_names
    assert len(first) == len(second) == MAX_MARK_NAME_LENGTH
    assert first != second


def test_a_quote_in_a_name_cannot_escape_the_attribute() -> None:
    document = build_ssml([{"text": "One.", "name": 'x" onload="boom'}])

    assert '"' not in document.mark_names[0]
    _parsed(document.ssml)


def test_generated_names_can_be_prefixed() -> None:
    document = build_ssml(["One.", "Two."], mark_prefix="beat")

    assert document.mark_names == ["beat0", "beat1"]


# -- the size limit ----------------------------------------------------------


def test_a_document_within_the_limit_is_built() -> None:
    document = build_ssml(["A short line."] * 10, max_bytes=MAX_INPUT_BYTES)

    assert document.size_bytes < MAX_INPUT_BYTES


def test_an_over_long_beat_is_refused_and_told_where_to_split() -> None:
    """ "5,000 bytes" is a number; "about the first 23 would fit" is an
    instruction. Truncating would produce a short that sounds complete and is
    missing its conclusion."""
    captions = ["A reasonably long sentence about compound interest."] * 200

    with pytest.raises(NodeConfigurationError) as caught:
        build_ssml(captions, max_bytes=MAX_INPUT_BYTES)

    message = str(caught.value)
    assert "200 captions" in message
    assert "split this beat" in message
    fitting = fit_phrases(captions, max_bytes=MAX_INPUT_BYTES)
    assert f"first {fitting}" in message


def test_the_reported_split_point_actually_fits() -> None:
    """The number in the error has to be true, or the author splits there and
    the next attempt fails the same way."""
    captions = ["Another sentence of roughly typical narration length."] * 200

    fitting = fit_phrases(captions, max_bytes=MAX_INPUT_BYTES)

    assert build_ssml(captions[:fitting], max_bytes=MAX_INPUT_BYTES).size_bytes <= MAX_INPUT_BYTES
    with pytest.raises(NodeConfigurationError):
        build_ssml(captions[: fitting + 1], max_bytes=MAX_INPUT_BYTES)


def test_the_limit_is_measured_on_the_tags_too() -> None:
    """The marks are part of the request Google measures. A limit checked
    against the phrase text alone would pass a document that is over it."""
    phrases = ["x" * 40] * 100
    text_only = sum(len(phrase) for phrase in phrases)
    document_size = build_ssml(phrases).size_bytes

    assert text_only < document_size


def test_the_limit_counts_utf8_bytes() -> None:
    """One Japanese character is three bytes; a limit counted in characters
    would let a document through at three times the size."""
    document = build_ssml(["あ" * 100])

    assert document.size_bytes > len(document.ssml)


# -- input that is not a phrase list -----------------------------------------


def test_an_empty_list_is_refused_by_name() -> None:
    with pytest.raises(NodeConfigurationError, match="caption list is empty"):
        build_ssml([])


def test_a_bare_string_is_refused_with_the_alternative_named() -> None:
    with pytest.raises(NodeConfigurationError, match="plain-text input"):
        build_ssml("One block of narration.")


def test_an_empty_phrase_is_refused() -> None:
    """A mark with no words under it is a caption that flashes and says
    nothing."""
    with pytest.raises(NodeConfigurationError, match="Caption 2 is empty"):
        build_ssml(["One.", "   ", "Three."])


def test_an_entry_that_is_not_text_says_which_one() -> None:
    with pytest.raises(NodeConfigurationError, match="Caption 2 is a int"):
        build_ssml(["One.", 2, "Three."])


def test_an_object_without_text_says_which_one() -> None:
    with pytest.raises(NodeConfigurationError, match="Caption 1 is an object with no 'text'"):
        build_ssml([{"name": "hook"}])


# -- `parse_phrases`, the part `openrouter_tts` reuses with no XML at all ----


def test_parse_phrases_agrees_with_build_ssml_on_names_and_text() -> None:
    """The extraction D9 exists for: a second provider gets the same phrase
    list, the same generated names, the same validation — without ever
    rendering a `<speak>` document it has no use for."""
    captions = ["First line.", {"text": "Second line.", "name": "beat-2"}]
    document = build_ssml(captions)
    phrases = parse_phrases(captions)

    assert [phrase.name for phrase in phrases] == document.mark_names
    assert [phrase.text for phrase in phrases] == [p.text for p in document.phrases]


def test_parse_phrases_raises_the_same_named_errors() -> None:
    with pytest.raises(NodeConfigurationError, match="Caption 2 is empty"):
        parse_phrases(["One.", "   ", "Three."])
