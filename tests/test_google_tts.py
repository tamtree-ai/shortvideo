"""`shortvideo.google_tts` — the request it sends and the answers it refuses.

Grouped by the four obligations Wave 1's "done when" names: a paragraph
becomes a playable attachment; marks are monotonic and inside the measured
duration; unsupported voice/mark combinations fail *by name*; and malformed or
oversized input is actionable. A fifth group covers §5.2's binding rule that a
paid call always reports a real cost, and §9's that a node never leaks a
credential or the media it handled.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tamtree_plugin_sdk import Item, NodeConfigurationError
from tamtree_plugin_sdk.testing import NodeTestKit

from tamtree_shortvideo.audio_duration import AudioDurationError
from tamtree_shortvideo.google_tts import (
    MAX_INPUT_BYTES,
    GoogleTtsNode,
    SynthesisError,
    SynthesisUnavailable,
    TimepointsUnsupported,
    mark_names,
)
from tests.audio_fixtures import MP3_FRAME_SECONDS, mp3_bytes, wav_bytes

TOKEN = "ya29.a0-a-real-looking-access-token"
VOICE = "en-US-Neural2-C"
NARRATION = "Three things nobody tells you about compound interest."

#: WaveNet/Neural2 list price at the time of writing — a *test* value, chosen
#: to make the arithmetic legible. The node ships no rate of its own on
#: purpose (§7: "No vendor price is copied into runtime defaults").
RATE = 16.0

DEFAULTS: dict[str, Any] = {
    "input_mode": "text",
    "text": NARRATION,
    "language_code": "en-US",
    "voice_name": VOICE,
    "ssml_gender": "SSML_VOICE_GENDER_UNSPECIFIED",
    "audio_encoding": "LINEAR16",
    "speaking_rate": 1.0,
    "pitch": 0,
    "volume_gain_db": 0,
    "sample_rate_hertz": 0,
    "output_binary_property": "audio",
    "price_usd_per_million_chars": RATE,
    "require_timepoints": True,
}


def _token_response() -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3599}
    )


def _synthesis_response(
    *,
    audio: bytes | None = None,
    timepoints: list[dict[str, Any]] | None = None,
    sample_rate: int = 24_000,
) -> httpx.Response:
    import base64

    body: dict[str, Any] = {
        "audioContent": base64.b64encode(
            wav_bytes(seconds=2.0) if audio is None else audio
        ).decode(),
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": sample_rate},
    }
    if timepoints is not None:
        body["timepoints"] = timepoints
    return httpx.Response(200, json=body)


def _kit(
    credential_payload: dict[str, str],
    *,
    responses: list[httpx.Response] | None = None,
    inputs: list[Item] | None = None,
    **overrides: Any,
) -> NodeTestKit:
    """A kit primed with the token mint's answer in front of the synthesis one —
    the node always mints before it speaks."""
    kit = (
        NodeTestKit(GoogleTtsNode())
        .params(**{**DEFAULTS, **overrides})
        .credentials({"google_service_account": credential_payload})
        .responses([_token_response(), *(responses or [_synthesis_response()])])
    )
    if inputs is not None:
        kit.inputs("main", inputs)
    return kit


def _request_body(kit: NodeTestKit, index: int = 1) -> dict[str, Any]:
    return json.loads(kit.requests[index].content.decode("utf-8"))


# -- a paragraph becomes a playable attachment -------------------------------


async def test_a_paragraph_becomes_an_audio_attachment_with_its_duration(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload)

    output = await kit.run()

    (item,) = output["main"]
    assert item.binary is not None
    ref = item.binary["audio"]
    assert ref.mime_type == "audio/wav"
    assert ref.size_bytes == len(wav_bytes(seconds=2.0))
    assert item.json_["duration_seconds"] == pytest.approx(2.0, abs=1e-6)
    assert item.json_["audio"]["encoding"] == "LINEAR16"
    assert item.json_["audio"]["sample_rate_hertz"] == 24_000
    assert item.json_["voice"] == {
        "language_code": "en-US",
        "name": VOICE,
        "ssml_gender": "SSML_VOICE_GENDER_UNSPECIFIED",
    }


async def test_it_calls_the_v1beta1_endpoint_with_a_bearer_token(
    credential_payload: dict[str, str],
) -> None:
    """v1beta1 is not incidental: `enableTimePointing` exists nowhere else, and
    caption timing is the reason this node is in the pipeline (D6)."""
    kit = _kit(credential_payload)

    await kit.run()

    request = kit.requests[1]
    assert str(request.url) == "https://texttospeech.googleapis.com/v1beta1/text:synthesize"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_the_source_json_and_any_existing_attachment_survive(
    credential_payload: dict[str, str],
) -> None:
    """§9's BinaryRef preservation, and §3's beat travelling with its audio: a
    beat arrives as `{narration, visual_prompt}` and must still carry both when
    it reaches the footage loop."""
    beat = Item.model_validate(
        {
            "json": {"narration": NARRATION, "visual_prompt": "a coin stack growing"},
            "binary": {
                "reference": {
                    "id": "b1",
                    "mime_type": "image/png",
                    "size_bytes": 3,
                    "storage_key": "ws/ws_test/binary/b1",
                }
            },
        }
    )
    kit = _kit(credential_payload, inputs=[beat])

    (item,) = (await kit.run())["main"]

    assert item.json_["visual_prompt"] == "a coin stack growing"
    assert item.binary is not None
    assert set(item.binary) == {"reference", "audio"}


async def test_one_output_item_per_input_item_and_one_token_mint(
    credential_payload: dict[str, str],
) -> None:
    """The mint is cached in process, so a flow narrating several beats signs
    once — `access_token` is called per node, not per item."""
    kit = _kit(
        credential_payload,
        responses=[_synthesis_response(), _synthesis_response()],
        inputs=[
            Item.model_validate({"json": {"beat": 1}}),
            Item.model_validate({"json": {"beat": 2}}),
        ],
    )

    output = await kit.run()

    assert [item.json_["beat"] for item in output["main"]] == [1, 2]
    assert len(kit.requests) == 3  # one mint, two syntheses


async def test_an_empty_input_still_narrates_the_configured_text(
    credential_payload: dict[str, str],
) -> None:
    (item,) = (await _kit(credential_payload).run())["main"]

    assert item.json_["duration_seconds"] > 0


async def test_the_attachment_name_is_configurable(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, output_binary_property="narration")

    (item,) = (await kit.run())["main"]

    assert item.binary is not None
    assert "narration" in item.binary
    assert item.json_["audio"]["binary_property"] == "narration"


async def test_an_mp3_request_is_measured_by_its_frames(
    credential_payload: dict[str, str],
) -> None:
    audio = mp3_bytes(frames=125)
    kit = _kit(
        credential_payload,
        audio_encoding="MP3",
        responses=[_synthesis_response(audio=audio)],
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["duration_seconds"] == pytest.approx(125 * MP3_FRAME_SECONDS, abs=1e-9)
    assert item.binary is not None
    assert item.binary["audio"].mime_type == "audio/mpeg"


# -- the request the node builds ---------------------------------------------


async def test_only_settings_that_differ_from_the_default_are_sent(
    credential_payload: dict[str, str],
) -> None:
    """A request that spells out every default hides which values the step
    actually chose, and pins Google's defaults to whatever they were today."""
    kit = _kit(credential_payload)

    await kit.run()

    assert _request_body(kit)["audioConfig"] == {"audioEncoding": "LINEAR16"}


async def test_audio_settings_reach_the_request_when_set(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        speaking_rate=1.15,
        pitch=-2.0,
        volume_gain_db=3.0,
        sample_rate_hertz=48_000,
    )

    await kit.run()

    assert _request_body(kit)["audioConfig"] == {
        "audioEncoding": "LINEAR16",
        "speakingRate": 1.15,
        "pitch": -2.0,
        "volumeGainDb": 3.0,
        "sampleRateHertz": 48_000,
    }


async def test_a_named_voice_wins_over_a_gender(credential_payload: dict[str, str]) -> None:
    """Google ignores `ssmlGender` once a name is given; sending both would
    make the request claim the gender chose the voice."""
    kit = _kit(credential_payload, ssml_gender="FEMALE")

    await kit.run()

    assert _request_body(kit)["voice"] == {"languageCode": "en-US", "name": VOICE}


async def test_a_gender_is_sent_when_no_voice_is_named(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, voice_name="", ssml_gender="FEMALE")

    await kit.run()

    assert _request_body(kit)["voice"] == {"languageCode": "en-US", "ssmlGender": "FEMALE"}


async def test_time_pointing_is_not_requested_for_unmarked_input(
    credential_payload: dict[str, str],
) -> None:
    """Asking for timepoints the input cannot produce invites an empty
    `timepoints` array that looks like a voice that dropped its marks."""
    kit = _kit(credential_payload)

    await kit.run()

    assert "enableTimePointing" not in _request_body(kit)


async def test_time_pointing_is_requested_when_the_ssml_has_marks(
    credential_payload: dict[str, str],
) -> None:
    ssml = '<speak><mark name="p0"/>Hello.</speak>'
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml=ssml,
        responses=[_synthesis_response(timepoints=[{"markName": "p0", "timeSeconds": 0.0}])],
    )

    await kit.run()

    body = _request_body(kit)
    assert body["enableTimePointing"] == ["SSML_MARK"]
    assert body["input"] == {"ssml": ssml}


# -- marks: normalized, ordered, and inside the audio -------------------------


async def test_timepoints_are_normalized_to_the_pipeline_vocabulary(
    credential_payload: dict[str, str],
) -> None:
    """`markName`/`timeSeconds` is Google's spelling. The compositor reads
    `{name, time_seconds}` whatever synthesized the audio (D9)."""
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One. <mark name="p1"/>Two.</speak>',
        responses=[
            _synthesis_response(
                timepoints=[
                    {"markName": "p0", "timeSeconds": 0.0},
                    {"markName": "p1", "timeSeconds": 0.84},
                ]
            )
        ],
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["marks"] == [
        {"name": "p0", "time_seconds": 0.0},
        {"name": "p1", "time_seconds": 0.84},
    ]


async def test_a_voice_that_drops_marks_fails_by_name(
    credential_payload: dict[str, str],
) -> None:
    """D6's actionable default. Studio voices do not support `<mark>`, and the
    symptom without this check is a finished short with no captions."""
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        voice_name="en-US-Studio-O",
        ssml='<speak><mark name="p0"/>One.</speak>',
        responses=[_synthesis_response(timepoints=[])],
    )

    with pytest.raises(TimepointsUnsupported) as caught:
        await kit.run()

    message = str(caught.value)
    assert "en-US-Studio-O" in message
    assert "p0" in message
    assert "Studio voices do not support it" in message


async def test_a_partially_answered_mark_set_fails_too(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One. <mark name="p1"/>Two.</speak>',
        responses=[_synthesis_response(timepoints=[{"markName": "p0", "timeSeconds": 0.0}])],
    )

    with pytest.raises(TimepointsUnsupported, match="1 of 2 caption marks"):
        await kit.run()


async def test_missing_marks_can_be_accepted_deliberately(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One.</speak>',
        require_timepoints=False,
        responses=[_synthesis_response(timepoints=[])],
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["marks"] == []


async def test_duplicate_mark_names_are_refused(credential_payload: dict[str, str]) -> None:
    """Google returns one timing per mark *occurrence*, so two marks sharing a
    name produce captions that cannot be matched back to their phrases."""
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One. <mark name="p0"/>Two.</speak>',
        responses=[_synthesis_response(timepoints=[{"markName": "p0", "timeSeconds": 0.0}])],
    )

    with pytest.raises(TimepointsUnsupported, match="reuses the mark name"):
        await kit.run()


async def test_marks_that_run_backwards_are_refused(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One. <mark name="p1"/>Two.</speak>',
        responses=[
            _synthesis_response(
                timepoints=[
                    {"markName": "p0", "timeSeconds": 1.2},
                    {"markName": "p1", "timeSeconds": 0.4},
                ]
            )
        ],
    )

    with pytest.raises(TimepointsUnsupported, match="before the mark ahead of it"):
        await kit.run()


async def test_a_mark_past_the_end_of_the_audio_is_refused(
    credential_payload: dict[str, str],
) -> None:
    """The audio is 2.0s; a caption at 9.5s would be shown over silence that
    does not exist."""
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One.</speak>',
        responses=[_synthesis_response(timepoints=[{"markName": "p0", "timeSeconds": 9.5}])],
    )

    with pytest.raises(TimepointsUnsupported, match="only 2.000s long"):
        await kit.run()


async def test_a_mark_at_the_very_end_is_within_tolerance(
    credential_payload: dict[str, str],
) -> None:
    """Google reports to the millisecond and the measurement is exact; a mark
    landing a hair past the end is rounding, not a contradiction."""
    kit = _kit(
        credential_payload,
        input_mode="ssml",
        ssml='<speak><mark name="p0"/>One.</speak>',
        responses=[_synthesis_response(timepoints=[{"markName": "p0", "timeSeconds": 2.02}])],
    )

    (item,) = (await kit.run())["main"]

    assert item.json_["marks"] == [{"name": "p0", "time_seconds": 2.02}]


@pytest.mark.parametrize(
    ("ssml", "expected"),
    [
        ('<speak><mark name="a"/>x</speak>', ["a"]),
        ("<speak><mark name='a'/>x</speak>", ["a"]),
        ('<speak><MARK NAME="a"/>x</speak>', ["a"]),
        ('<speak><mark time="1s" name="a"/>x</speak>', ["a"]),
        ('<speak><mark name="a"/>x<mark name="b"/>y</speak>', ["a", "b"]),
        ("<speak>no marks here</speak>", []),
        ('<speak><remark name="a"/>x</speak>', []),
    ],
)
def test_mark_names_reads_what_google_will_report(ssml: str, expected: list[str]) -> None:
    assert mark_names(ssml) == expected


# -- input that is refused before a penny is spent ---------------------------


async def test_an_oversized_request_is_refused_without_calling_google(
    credential_payload: dict[str, str],
) -> None:
    """Truncating to fit would drop the end of the narration and the short
    would never mention it — V1.3's "fail or split deliberately"."""
    kit = _kit(credential_payload, text="a" * (MAX_INPUT_BYTES + 1))

    with pytest.raises(NodeConfigurationError, match="synchronous synthesize limit"):
        await kit.run()

    assert len(kit.requests) == 1  # the mint only; nothing was synthesized


async def test_the_limit_counts_utf8_bytes_not_characters(
    credential_payload: dict[str, str],
) -> None:
    """ "In some locales a single character is made up of multiple bytes" —
    2,000 Japanese characters are 6,000 bytes and over the line."""
    kit = _kit(credential_payload, text="あ" * 2_000)

    with pytest.raises(NodeConfigurationError, match="6,000 UTF-8 bytes"):
        await kit.run()


async def test_empty_narration_is_refused_by_name(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, text="   ")

    with pytest.raises(NodeConfigurationError, match="Text is empty"):
        await kit.run()


async def test_an_unmeasurable_audio_format_is_refused_up_front(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, audio_encoding="M4A")

    with pytest.raises(NodeConfigurationError, match="not offered"):
        await kit.run()

    assert len(kit.requests) == 1


async def test_a_non_numeric_speaking_rate_says_which_setting_is_wrong(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, speaking_rate="fast")

    with pytest.raises(NodeConfigurationError, match="'speaking_rate' must be a number"):
        await kit.run()


# -- cost: §5.2's binding rule ------------------------------------------------


async def test_every_call_reports_a_real_cost(credential_payload: dict[str, str]) -> None:
    """A paid call reported without a cost is invisible to the workspace
    monthly budget *and* counted against `unpriced_block_count`."""
    kit = _kit(credential_payload)
    ctx = kit.context()

    await GoogleTtsNode().execute(ctx)

    (usage,) = ctx.usage
    assert usage["provider"] == "google"
    assert usage["model"] == VOICE
    assert usage["cost_usd"] == Decimal(len(NARRATION)) * Decimal("16") / Decimal(1_000_000)
    # A synthesis call has no tokens; the figure rides in `cost_usd` (§5.2).
    assert usage["tokens_in"] == 0
    assert usage["tokens_out"] == 0


async def test_the_cost_is_exact_decimal_arithmetic(
    credential_payload: dict[str, str],
) -> None:
    """A rate the editor hands over as a float must not drift on the way to
    the ledger: `Decimal(0.1)` is 0.1000000000000000055511151231257827, and
    money summed from figures like that does not reconcile."""
    kit = _kit(credential_payload, text="a" * 4_000, price_usd_per_million_chars=0.1)
    ctx = kit.context()

    await GoogleTtsNode().execute(ctx)

    assert ctx.usage[0]["cost_usd"] == Decimal("0.00040000")


async def test_a_missing_price_refuses_the_call_before_it_is_billed(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, price_usd_per_million_chars=0)

    with pytest.raises(NodeConfigurationError) as caught:
        await kit.run()

    assert "text-to-speech/pricing" in str(caught.value)
    assert len(kit.requests) == 1  # nothing was synthesized


async def test_no_rate_is_built_into_the_node() -> None:
    """§7: "No vendor price is copied into runtime defaults." A default here
    would go stale and quietly under-report real spend."""
    (param,) = [
        field
        for field in GoogleTtsNode().manifest.params
        if field.name == "price_usd_per_million_chars"
    ]
    assert param.required is True
    assert not param.default


# -- provider failures, split by whether a retry could help -------------------


async def test_a_bad_request_is_named_and_not_retried(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        responses=[
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "Voice 'en-US-Nope' does not exist.",
                    }
                },
            )
        ],
    )

    with pytest.raises(SynthesisError) as caught:
        await kit.run()

    assert "INVALID_ARGUMENT" in str(caught.value)
    assert "does not exist" in str(caught.value)
    assert isinstance(caught.value, NodeConfigurationError)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_quota_or_outage_keeps_its_retry_budget(
    credential_payload: dict[str, str], status: int
) -> None:
    """A 429 clears on its own and a 5xx is exactly what a retry is for, so
    neither is a `NodeConfigurationError` — which is what makes a step
    non-retryable."""
    kit = _kit(credential_payload, responses=[httpx.Response(status, json={})])

    with pytest.raises(SynthesisUnavailable) as caught:
        await kit.run()

    assert not isinstance(caught.value, NodeConfigurationError)


async def test_a_dropped_connection_keeps_its_retry_budget(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload)
    transport = httpx.MockTransport(
        lambda request: (
            (_ for _ in ()).throw(httpx.ConnectError("reset"))
            if "texttospeech" in str(request.url)
            else _token_response()
        )
    )
    ctx = kit.context()
    ctx.http = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]

    with pytest.raises(SynthesisUnavailable, match="another attempt"):
        await GoogleTtsNode().execute(ctx)


async def test_a_response_with_no_audio_is_named(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, responses=[httpx.Response(200, json={"audioConfig": {}})])

    with pytest.raises(SynthesisError, match="without any audio content"):
        await kit.run()


async def test_audio_that_is_not_base64_is_named(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(
        credential_payload,
        responses=[httpx.Response(200, json={"audioContent": "not base64 !!"})],
    )

    with pytest.raises(SynthesisError, match="not valid base64"):
        await kit.run()


async def test_audio_that_cannot_be_measured_is_named(
    credential_payload: dict[str, str],
) -> None:
    kit = _kit(credential_payload, responses=[_synthesis_response(audio=b"\x11" * 500)])

    with pytest.raises(AudioDurationError, match="without the WAV header"):
        await kit.run()


# -- nothing leaks (§9 / R9) --------------------------------------------------


async def test_neither_the_key_nor_the_token_nor_the_audio_reaches_a_log(
    credential_payload: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R9: the private key, the bearer token and the synthesized media are all
    things a worker log must never carry."""
    caplog.set_level(logging.DEBUG)
    key_file = json.loads(credential_payload["service_account_json"])

    await _kit(credential_payload).run()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert key_file["private_key"] not in logged
    assert TOKEN not in logged
    assert "audioContent" not in logged


async def test_the_bearer_token_is_not_repeated_into_the_output_item(
    credential_payload: dict[str, str],
) -> None:
    (item,) = (await _kit(credential_payload).run())["main"]

    assert TOKEN not in json.dumps(item.json_)


async def test_a_refusal_quotes_google_but_not_the_credential(
    credential_payload: dict[str, str],
) -> None:
    key_file = json.loads(credential_payload["service_account_json"])
    kit = _kit(
        credential_payload,
        responses=[httpx.Response(403, json={"error": {"message": "Permission denied."}})],
    )

    with pytest.raises(SynthesisError) as caught:
        await kit.run()

    message = str(caught.value)
    assert "Permission denied." in message
    assert key_file["private_key"] not in message
    assert TOKEN not in message
