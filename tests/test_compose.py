"""V3.3 acceptance: `shortvideo.compose` turns a timeline into one render call.

The node itself renders nothing — `CuratedCliToolRuntime` does, in a sandbox
these tests deliberately do not stand up. What is testable here is everything
that decides *whether the render is the right one*, and that is the part a
browser could never tell you was wrong:

- validation happens before any byte is fetched (D9's promise is about cost),
- every ref resolves, or the step fails naming the ones that did not,
- the render document names the paths the runtime is about to create,
- and nothing identifying — a ref id, a workspace, a provider's file name —
  reaches the child.

The last one is the one worth writing carefully: the inverse assertion (that
the document *contains* the paths) would pass on a document that also leaked
every ref id beside them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from tamtree_plugin_sdk import BinaryRef, Item, NodeConfigurationError, ToolResult, ToolSpec
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.compose import ComposeNode, WorkspaceRenderGate, materialized_name
from tamtree_shortvideo.timeline import (
    Beat,
    Caption,
    Clip,
    Music,
    Narration,
    Timeline,
    Transition,
    as_json,
    timeline_digest,
)

FPS = 30


@pytest.fixture(autouse=True)
def _no_licence_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own key must not turn these into network tests."""
    monkeypatch.delenv("TAMTREE_REMOTION_LICENSE_KEY", raising=False)


NARRATION_ID = "bin_01J8XKNARR"


def _clip(index: int, *, seconds: float) -> Clip:
    return Clip(
        ref_id=f"bin_01J8XK{index}CLIP",
        mime_type="video/mp4",
        source_duration_seconds=seconds + 1.0,
        in_seconds=0.0,
        out_seconds=seconds,
    )


def _timeline(
    *, beat_frames: tuple[int, ...] = (60, 90, 60), template: str = "short-captioned"
) -> Timeline:
    beats: list[Beat] = []
    start = 0
    for index, frames in enumerate(beat_frames):
        beats.append(
            Beat(
                index=index,
                start_frame=start,
                frames=frames,
                clip=_clip(index, seconds=frames / FPS),
                transition=Transition(kind="crossfade", frames=6) if index else Transition(),
                captions=(
                    (Caption(text=f"Beat {index}.", start_frame=start, end_frame=start + frames),)
                    if template == "short-captioned"
                    else ()
                ),
            )
        )
        start += frames
    return Timeline(
        template=template,  # type: ignore[arg-type]
        narration=Narration(
            ref_id=NARRATION_ID, mime_type="audio/wav", duration_seconds=start / FPS
        ),
        beats=tuple(beats),
    )


def _ref(ref_id: str, *, mime: str, name: str) -> BinaryRef:
    return BinaryRef(
        id=ref_id,
        file_name=name,
        mime_type=mime,
        size_bytes=1024,
        storage_key=f"ws/ws_test/binary/{ref_id}",
    )


class _RecordingRuntime:
    """A `ToolRuntime` that records each call and answers it the way its backend
    would — a normalised wav from `shortvideo-audio`, a rendered mp4 from
    `remotion`. The sandbox is not under test here; what the node *asked for* is.

    `calls` holds renders only and `loudness_calls` the normalisation passes,
    so a test about the render never has to count its way past the audio."""

    name = "curated"

    def __init__(
        self,
        *,
        ok: bool = True,
        error: str | None = None,
        audio_ok: bool = True,
        audio_error: str | None = None,
    ) -> None:
        self.calls: list[tuple[ToolSpec, dict[str, Any]]] = []
        self.loudness_calls: list[tuple[ToolSpec, dict[str, Any]]] = []
        #: Every call, both backends, in the order the node made them.
        self.order: list[str] = []
        self._ok = ok
        self._error = error
        self._audio_ok = audio_ok
        self._audio_error = audio_error
        self.concurrent = 0
        self.peak = 0

    async def execute(
        self,
        tool: ToolSpec,
        input: dict[str, Any],
        *,
        workspace_id: str,
        idempotency_key: str | None,
        limits: Any,
        binary_store: Any = None,
    ) -> ToolResult:
        self.order.append(input["backend"])
        if input["backend"] == "shortvideo-audio":
            self.loudness_calls.append((tool, input))
            if not self._audio_ok:
                return ToolResult(ok=False, output=None, error=self._audio_error, duration_ms=1)
            ref = await binary_store.put_binary(b"RIFF normalised", "audio/wav", "output.wav")
            return ToolResult(
                ok=True,
                output={"backend": "shortvideo-audio", "preset": "loudnorm"},
                error=None,
                duration_ms=210,
                binary={"audio": ref},
            )
        self.calls.append((tool, input))
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            await asyncio.sleep(0)
            if not self._ok:
                return ToolResult(ok=False, output=None, error=self._error, duration_ms=1)
            ref = await binary_store.put_binary(b"rendered mp4 bytes", "video/mp4", "output.mp4")
            return ToolResult(
                ok=True,
                output={"backend": "remotion", "preset": "compose"},
                error=None,
                duration_ms=4321,
                binary={"video": ref},
            )
        finally:
            self.concurrent -= 1


def _ctx(
    timeline: Timeline | None = None,
    *,
    runtime: _RecordingRuntime | None = None,
    attachments: bool = True,
    **params: Any,
) -> FakeExecutionContext:
    timeline = timeline or _timeline()
    binary: dict[str, BinaryRef] = {}
    if attachments:
        binary[NARRATION_ID] = _ref(NARRATION_ID, mime="audio/wav", name="narration-final.wav")
        if timeline.music is not None:
            binary[timeline.music.ref_id] = _ref(
                timeline.music.ref_id, mime=timeline.music.mime_type, name="bed.mp3"
            )
        for beat in timeline.beats:
            binary[beat.clip.ref_id] = _ref(
                beat.clip.ref_id, mime="video/mp4", name=f"minimax-{beat.clip.ref_id}.mp4"
            )
    return FakeExecutionContext(
        inputs={
            "main": [
                Item.model_validate({"json": {"timeline": as_json(timeline)}, "binary": binary})
            ]
        },
        params=params,
        tool_runtime=runtime or _RecordingRuntime(),
    )


async def _run(ctx: FakeExecutionContext) -> dict[str, list[Item]]:
    return await ComposeNode().execute(ctx)


# --- the happy path, and what it proves ------------------------------------


async def test_a_valid_timeline_renders_and_returns_the_attachment() -> None:
    runtime = _RecordingRuntime()
    result = await _run(_ctx(runtime=runtime))
    item = result["main"][0]

    assert "video" in (item.binary or {})
    assert item.json_["digest"] == timeline_digest(_timeline())
    assert item.json_["frames"] == 210
    assert item.json_["duration_seconds"] == 7.0
    assert item.json_["beats"] == 3
    assert item.json_["draft"] is False
    assert item.json_["duration_ms"] == 4321

    tool, sent = runtime.calls[0]
    assert tool.kind == "curated" and tool.entrypoint == "remotion"
    assert sent["preset"] == "compose"
    assert sent["params"] == {"scale": 1.0, "concurrency": 1}


async def test_the_timeline_document_is_the_first_input() -> None:
    """`ComposePreset` names `inputs[0]` as the document without counting, so
    the order is a contract between the node and the preset rather than a
    convention either could change alone."""
    runtime = _RecordingRuntime()
    await _run(_ctx(runtime=runtime))
    _, sent = runtime.calls[0]
    first = sent["inputs"][0]
    assert first["mime_type"] == "application/json"
    assert first["file_name"] == "timeline.json"


async def test_media_follows_in_the_order_the_document_names_it() -> None:
    runtime = _RecordingRuntime()
    await _run(_ctx(runtime=runtime))
    _, sent = runtime.calls[0]
    names = [ref["file_name"] for ref in sent["inputs"]]
    assert names == [
        "timeline.json",
        "narration.wav",
        "clip-000.mp4",
        "clip-001.mp4",
        "clip-002.mp4",
    ]


async def test_the_render_document_names_the_paths_the_runtime_will_create() -> None:
    """The node builds the document *before* materialization, so it has to
    predict where each input lands. `materialized_name` is that prediction, and
    this is what keeps it honest against the refs actually sent."""
    runtime = _RecordingRuntime()
    ctx = _ctx(runtime=runtime)
    await _run(ctx)
    _, sent = runtime.calls[0]

    document = json.loads(await ctx.get_binary(BinaryRef.model_validate(sent["inputs"][0])))
    expected = [
        materialized_name(index, BinaryRef.model_validate(ref))
        for index, ref in enumerate(sent["inputs"])
    ]
    assert document["narration"]["file"] == expected[1]
    assert [beat["clip"]["file"] for beat in document["beats"]] == expected[2:]


async def test_the_child_never_learns_a_ref_id_or_a_provider_filename() -> None:
    """Written as "no identifier appears anywhere in the document" rather than
    "the paths are right", because the second passes on a document that carries
    both."""
    runtime = _RecordingRuntime()
    ctx = _ctx(runtime=runtime)
    await _run(ctx)
    _, sent = runtime.calls[0]
    raw = (await ctx.get_binary(BinaryRef.model_validate(sent["inputs"][0]))).decode()

    timeline = _timeline()
    for ref_id in [timeline.narration.ref_id, *(beat.clip.ref_id for beat in timeline.beats)]:
        assert ref_id not in raw
    assert "minimax-" not in raw  # the provider's own file names
    assert "ws_test" not in raw  # the workspace
    assert '"ref"' not in raw


async def test_the_frozen_mix_numbers_ride_the_document() -> None:
    """Loudness is data, not a render-time default, so one document fully
    determines one render."""
    runtime = _RecordingRuntime()
    ctx = _ctx(runtime=runtime)
    await _run(ctx)
    _, sent = runtime.calls[0]
    document = json.loads(await ctx.get_binary(BinaryRef.model_validate(sent["inputs"][0])))
    assert document["narration"]["target_lufs"] == -16.0
    assert document["narration"]["true_peak_ceiling_dbtp"] == -1.5
    assert document["clip_duck_db"] == -18.0
    assert document["caption_safe_area"] == {"inset_x": 0.08, "top": 0.66, "bottom": 0.84}


async def test_a_draft_differs_only_in_scale() -> None:
    runtime = _RecordingRuntime()
    result = await _run(_ctx(runtime=runtime, draft=True))
    _, sent = runtime.calls[0]
    assert sent["params"]["scale"] == 0.5
    item = result["main"][0]
    assert item.json_["draft"] is True
    assert (item.json_["width"], item.json_["height"]) == (540, 960)
    # The frame count — what the approval is actually about — is untouched.
    assert item.json_["frames"] == 210


async def test_the_output_attachment_can_be_renamed() -> None:
    result = await _run(_ctx(attachment="final_cut"))
    assert "final_cut" in (result["main"][0].binary or {})


# --- loudness: §7's numbers, applied before the render ----------------------


async def test_narration_is_normalised_to_the_frozen_target_before_the_render() -> None:
    """The renderer cannot measure integrated loudness, so if this pass did not
    happen nothing would apply §7 — and the render would still look green."""
    runtime = _RecordingRuntime()
    await _run(_ctx(runtime=runtime))
    assert runtime.order == ["shortvideo-audio", "remotion"]
    tool, sent = runtime.loudness_calls[0]
    assert tool.entrypoint == "shortvideo-audio"
    assert sent["preset"] == "loudnorm"
    assert sent["params"] == {"target_lufs": -16.0, "true_peak_dbtp": -1.5}
    assert [ref["id"] for ref in sent["inputs"]] == [NARRATION_ID]


async def test_the_render_gets_the_normalised_narration_not_the_original() -> None:
    """Written against the ref id rather than the file name, because the node
    renames both to `narration.wav` — a name check would pass on either."""
    runtime = _RecordingRuntime()
    ctx = _ctx(runtime=runtime)
    await _run(ctx)
    _, sent = runtime.calls[0]
    narration = sent["inputs"][1]
    assert narration["file_name"] == "narration.wav"
    assert narration["id"] != NARRATION_ID
    assert await ctx.get_binary(BinaryRef.model_validate(narration)) == b"RIFF normalised"


async def test_a_music_bed_gets_its_own_pass_at_its_own_target() -> None:
    timeline = _timeline()
    music_id = "bin_01J8XKMUSIC"
    timeline = dataclasses.replace(
        timeline,
        music=Music(ref_id=music_id, mime_type="audio/mpeg", duration_seconds=60.0),
    )
    runtime = _RecordingRuntime()
    await _run(_ctx(timeline, runtime=runtime))

    targets = {sent["inputs"][0]["id"]: sent["params"] for _, sent in runtime.loudness_calls}
    assert targets == {
        NARRATION_ID: {"target_lufs": -16.0, "true_peak_dbtp": -1.5},
        music_id: {"target_lufs": -20.0, "true_peak_dbtp": -1.5},
    }
    _, sent = runtime.calls[0]
    music = sent["inputs"][2]
    assert music["file_name"] == "music.wav"  # the normalised wav, renamed
    assert music["id"] != music_id


async def test_a_failed_normalisation_fails_the_step_and_nothing_renders() -> None:
    """Rendering at whatever level the provider delivered would be the silent
    version of this failure — the one §7 exists to prevent."""
    runtime = _RecordingRuntime(audio_ok=False, audio_error="ffmpeg is not installed")
    with pytest.raises(RuntimeError, match="normalise the narration loudness.*not installed"):
        await _run(_ctx(runtime=runtime))
    assert runtime.calls == []


async def test_a_render_with_no_licence_key_still_renders_and_says_it_reported_nothing() -> None:
    """V0.4's binding consequence: failing the render would be worse than the
    compliance gap it tries to prevent — but the gap is stated, per render."""
    result = await _run(_ctx())
    out = result["main"][0].json_
    assert out["remotion_usage_report"] == "not_configured"
    assert "TAMTREE_REMOTION_LICENSE_KEY" in out["remotion_usage_detail"]
    assert "video" in (result["main"][0].binary or {})


# --- what it refuses, and how early ----------------------------------------


async def test_an_invalid_timeline_is_refused_before_anything_is_fetched() -> None:
    """D9: rejects before spawning Chrome. Here, before spawning anything."""
    broken = as_json(_timeline())
    broken["beats"][1]["start_frame"] = 999  # a gap the validator must catch
    runtime = _RecordingRuntime()
    ctx = FakeExecutionContext(
        inputs={"main": [Item.model_validate({"json": {"timeline": broken}})]},
        params={},
        tool_runtime=runtime,
    )
    with pytest.raises(NodeConfigurationError):
        await _run(ctx)
    assert runtime.calls == []
    assert runtime.loudness_calls == []


async def test_a_ref_no_item_carries_is_named_rather_than_missing_later() -> None:
    runtime = _RecordingRuntime()
    with pytest.raises(NodeConfigurationError, match="bin_01J8XK1CLIP"):
        await _run(_ctx(runtime=runtime, attachments=False))
    assert runtime.calls == []
    assert runtime.loudness_calls == []


async def test_a_blank_timeline_with_no_item_says_so() -> None:
    ctx = FakeExecutionContext(inputs={"main": []}, params={}, tool_runtime=_RecordingRuntime())
    with pytest.raises(NodeConfigurationError, match="no timeline"):
        await _run(ctx)


async def test_a_timeline_that_is_not_json_is_a_configuration_error() -> None:
    ctx = FakeExecutionContext(
        inputs={"main": [Item.model_validate({"json": {}})]},
        params={"timeline": "{not json"},
        tool_runtime=_RecordingRuntime(),
    )
    with pytest.raises(NodeConfigurationError, match="not valid JSON"):
        await _run(ctx)


async def test_a_failed_render_carries_the_runtime_s_own_sentence() -> None:
    runtime = _RecordingRuntime(ok=False, error="curated render exceeded its 900s time limit")
    with pytest.raises(RuntimeError, match="900s time limit"):
        await _run(_ctx(runtime=runtime))


async def test_the_timeline_can_come_from_the_parameter_instead_of_the_item() -> None:
    """An expression resolves to text, so a JSON string is accepted as well as
    an object — the template wires `{{ $json.timeline }}`, which is either."""
    timeline = _timeline(template="short-plain")
    binary = {NARRATION_ID: _ref(NARRATION_ID, mime="audio/wav", name="n.wav")}
    for beat in timeline.beats:
        binary[beat.clip.ref_id] = _ref(beat.clip.ref_id, mime="video/mp4", name="c.mp4")
    ctx = FakeExecutionContext(
        inputs={"main": [Item.model_validate({"json": {}, "binary": binary})]},
        params={"timeline": json.dumps(as_json(timeline))},
        tool_runtime=_RecordingRuntime(),
    )
    result = await _run(ctx)
    assert result["main"][0].json_["template"] == "short-plain"


# --- the gate, and what it is not ------------------------------------------


async def test_renders_are_serialised_per_workspace_on_this_worker() -> None:
    """A burst waits for a slot rather than failing. This is a *process-local*
    cap — not tenant fairness, and not a deployment-wide limit: a second worker
    has its own gate and knows nothing about this one."""
    gate = WorkspaceRenderGate(1)
    peak = 0
    live = 0

    async def occupy() -> None:
        nonlocal peak, live
        async with gate.slot("ws_test"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(occupy() for _ in range(4)))
    assert peak == 1


async def test_different_workspaces_do_not_queue_behind_each_other() -> None:
    gate = WorkspaceRenderGate(1)
    order: list[str] = []

    async def occupy(workspace: str) -> None:
        async with gate.slot(workspace):
            order.append(workspace)
            await asyncio.sleep(0.01)

    await asyncio.gather(occupy("ws_a"), occupy("ws_b"))
    assert set(order) == {"ws_a", "ws_b"}


# --- the path prediction itself --------------------------------------------


def test_materialized_name_takes_the_extension_from_the_name_we_set() -> None:
    assert materialized_name(2, _ref("x", mime="video/mp4", name="clip-000.mp4")) == "_in/2.mp4"
    assert (
        materialized_name(0, _ref("x", mime="application/json", name="timeline.json"))
        == "_in/0.json"
    )


def test_materialized_name_falls_back_rather_than_guessing_wildly() -> None:
    """An unusable name yields `.bin`, which is what the runtime does too. The
    node sets the names itself precisely so this branch is unreachable in
    practice — it exists so a drift produces a missing file, not a wrong one."""
    assert materialized_name(1, _ref("x", mime="video/mp4", name="")) == "_in/1.bin"
    assert materialized_name(1, _ref("x", mime="video/mp4", name="no-suffix")) == "_in/1.bin"
