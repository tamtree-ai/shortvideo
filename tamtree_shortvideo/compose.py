"""`shortvideo.compose` — turn one validated `TimelineV1` into one mp4 (V3.3).

The node is deliberately thin, because everything expensive about it has been
decided somewhere else: `timeline.py` says what a legal timeline is,
`remotion.py` says what a render costs and what argv performs it, and
`CuratedCliToolRuntime` owns isolation. What is left here is the part that
genuinely belongs to a node — resolving refs, deciding the order inputs are
materialized in, and turning a failed render into a sentence an author can act
on.

**The order it does things in is the design.** Validation first, against the
authoring form, before a single byte is fetched: D9's "rejects missing refs,
non-monotonic marks, overlaps, unsupported codecs and a duration above the v1
ceiling *before spawning Chrome*" is a promise about cost, and it is only true
if nothing expensive happens first. Then refs are resolved — all of them, so a
timeline naming an attachment this run never produced fails at the ref rather
than three layers down inside a browser. Then each audio track is brought to
its §7 loudness target by the `shortvideo-audio` backend — the one thing the
renderer cannot do. Only then is the render document built, and only then does
Chrome spawn.

**The concurrency gate is per worker process, and this file will not call it
anything else.** At most `TAMTREE_SHORTVIDEO_MAX_RENDERS` (default 1) renders
per workspace run concurrently *on this worker*; a burst waits for a slot
rather than failing. It is not tenant fairness and it is not a deployment-wide
cap — a second worker has its own gate and knows nothing about this one. The
number an operator sizes with is **~880 MB of RSS and ~1.7 cores per
concurrent render** (V0.3, measured). A cluster-wide limiter is the right
answer the moment video stops being self-hosted-only, which is the same
boundary SEC-D3 already draws.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, Final

from tamtree_plugin_sdk import (
    BinaryRef,
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
    ToolSpec,
)

from tamtree_shortvideo import loudness
from tamtree_shortvideo.licensing import report_render
from tamtree_shortvideo.remotion import BACKEND_ID, PRESET_NAME, RENDER_MEMORY_MB
from tamtree_shortvideo.timeline import (
    MUSIC_TARGET_LUFS,
    NARRATION_TARGET_LUFS,
    NARRATION_TRUE_PEAK_DBTP,
    TimelineError,
    as_json,
    render_document,
    timeline_digest,
    timeline_from_json,
    validate,
)

__all__ = ["NODE_NAME", "ComposeNode", "WorkspaceRenderGate", "materialized_name"]

NODE_NAME: Final = "shortvideo.compose"

#: Renders per workspace, per worker process. One by default because a render
#: is ~880 MB and ~1.7 cores, so the second concurrent one on a 2 GB worker is
#: the one that gets the OOM killer rather than a queue.
_MAX_RENDERS_ENV: Final = "TAMTREE_SHORTVIDEO_MAX_RENDERS"


def _max_concurrent() -> int:
    raw = os.environ.get(_MAX_RENDERS_ENV)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 1


class WorkspaceRenderGate:
    """At most `cap` concurrent renders per workspace **on this worker process**.

    The same shape as `tamtree.media`'s `WorkspaceConcurrencyGate`, written
    here rather than imported because that class lives in `tamtree_nodes`,
    which a published plugin must not depend on (§20.2). A render over the cap
    *awaits* a slot rather than failing, so a burst is serialised, not dropped.
    """

    def __init__(self, cap: int) -> None:
        self._cap = max(1, cap)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._guard = asyncio.Lock()

    async def _sem(self, workspace_id: str) -> asyncio.Semaphore:
        async with self._guard:
            return self._sems.setdefault(workspace_id, asyncio.Semaphore(self._cap))

    @contextlib.asynccontextmanager
    async def slot(self, workspace_id: str) -> AsyncIterator[None]:
        sem = await self._sem(workspace_id)
        async with sem:
            yield


def _last_line(error: str | None) -> str:
    """The part of a runtime error an author can act on.

    The runtime reports a failed render as `curated render failed: ` plus the
    last 600 characters of the child's stderr — and Remotion writes an object
    dump there before the renderer gets its turn. `tamtree-remotion-render`
    guarantees its final stderr line is one sentence (`src/report.mjs`), so the
    last line is the sentence and everything above it is noise to an author.
    An error with no such tail — a timeout, a size cap, a hosted refusal — is
    one line already and passes through unchanged.
    """
    text = (error or "unknown error").strip()
    prefix, marker, tail = text.partition("curated render failed: ")
    if not marker:
        return text
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return f"{prefix}{marker}{lines[-1]}" if lines else text


def materialized_name(index: int, ref: BinaryRef) -> str:
    """The workdir-relative path `CuratedCliToolRuntime` will materialize
    `inputs[index]` to.

    **This mirrors a rule in the runtime, and the mirror is deliberate rather
    than accidental.** The runtime names a materialized input `_in/<index>` plus
    an extension taken from the ref's `file_name`, and the render document has
    to name the same paths — it is built before materialization happens, so the
    node has to know where the bytes will land.

    The coupling is made safe by controlling the only input to the rule: the
    node hands the runtime refs whose `file_name` it has set itself, to a
    deterministic `clip-000.mp4` / `narration.wav` / `timeline.json`, so the
    extension cannot depend on whatever a provider happened to call a file.
    And the failure mode if the rule ever changes is a *missing file the
    renderer names*, not a swapped clip: indices come from the node's own
    ordering, so the only thing a drift can break is the suffix.
    """
    name = ref.file_name or ""
    _, dot, suffix = name.rpartition(".")
    extension = (
        f".{suffix.lower()}" if dot and 1 <= len(suffix) <= 5 and suffix.isalnum() else ".bin"
    )
    return f"_in/{index}{extension}"


class ComposeNode(ProgrammaticNode):
    """Render an approved timeline to a vertical mp4."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — compose",
            "description": (
                "Render a TimelineV1 document to a 1080×1920 mp4 — narration, per-beat "
                "footage, captions and transitions. Self-hosted only: the renderer runs "
                "in the local sandbox, which hosted multi-tenant does not support."
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
                            "digest": {"type": "string"},
                            "template": {"type": "string"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                            "fps": {"type": "number"},
                            "frames": {"type": "number"},
                            "duration_seconds": {"type": "number"},
                            "scale": {"type": "number"},
                            "draft": {"type": "boolean"},
                            "beats": {"type": "number"},
                            "duration_ms": {"type": "number"},
                            "remotion_usage_report": {
                                "type": "string",
                                "enum": ["not_configured", "reported", "failed"],
                            },
                            "remotion_usage_detail": {"type": "string"},
                            "timeline": {"type": "object"},
                            "attachment": {"type": "string"},
                        },
                        "required": ["digest", "template", "frames", "duration_seconds"],
                    },
                }
            ],
            "params": [
                {
                    "name": "timeline",
                    "label": "Timeline",
                    "type": "json",
                    "default": "",
                    "description": (
                        "The TimelineV1 document to render. Leave blank to read it from the "
                        "incoming item's `timeline` field — {{ $json.timeline }} — which is "
                        "what the Short-form video template wires up."
                    ),
                },
                {
                    "name": "draft",
                    "label": "Draft quality",
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Render at half size (540×960) for the approval preview. The same "
                        "composition and the same frame boundaries — scale is the only "
                        "difference, so approving the draft and rendering the final are "
                        "the same decision."
                    ),
                },
                {
                    "name": "expected_digest",
                    "label": "Approved timeline digest",
                    "type": "string",
                    "default": "",
                    "description": (
                        "Refuse to render unless the timeline's digest is exactly this — map it "
                        "from the draft that was approved, e.g. {{ $node('draft').items[0]"
                        ".json['digest'] }}, so the final render is provably the approved one. "
                        "Blank skips the check."
                    ),
                },
                {
                    "name": "attachment",
                    "label": "Output attachment name",
                    "type": "string",
                    "default": "video",
                    "description": "The attachment the finished mp4 is returned under.",
                },
            ],
        }
    )

    #: One gate per process, so the per-workspace cap holds across every
    #: compose node running on this worker.
    _gate: ClassVar[WorkspaceRenderGate] = WorkspaceRenderGate(_max_concurrent())

    def _timeline_document(self, ctx: ExecutionContext) -> Mapping[str, Any]:
        """The timeline, from the param if it was given and from the incoming
        item if it was not. A string is accepted because an expression that
        resolves to JSON arrives as text."""
        raw: Any = ctx.param("timeline")
        if isinstance(raw, str):
            raw = raw.strip()
        if not raw:
            items = ctx.input_items()
            if not items:
                raise NodeConfigurationError(
                    "Short video compose has no timeline: the Timeline parameter is blank and "
                    "no item arrived to read one from."
                )
            raw = (items[0].json_ or {}).get("timeline")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as error:
                raise NodeConfigurationError(
                    f"Short video compose: the timeline is not valid JSON — {error}"
                ) from error
        if not isinstance(raw, Mapping):
            raise NodeConfigurationError(
                "Short video compose: the timeline must be a TimelineV1 object, got "
                f"{type(raw).__name__}."
            )
        return raw

    def _resolve_refs(
        self, ctx: ExecutionContext, wanted: set[str]
    ) -> tuple[dict[str, BinaryRef], dict[str, BinaryRef]]:
        """Every `BinaryRef` the timeline names, found among the attachments on
        the incoming items — keyed by ref id, and by the attachment name it
        arrived under (which is what the output carries forward). A ref the run
        never produced is a rejection here — before any bytes are fetched, and
        long before Chrome exists."""
        found: dict[str, BinaryRef] = {}
        named: dict[str, BinaryRef] = {}
        for item in ctx.input_items():
            for attachment, ref in (item.binary or {}).items():
                if ref.id in wanted and ref.id not in found:
                    found[ref.id] = ref
                    named.setdefault(attachment, ref)
        missing = sorted(wanted - set(found))
        if missing:
            raise NodeConfigurationError(
                "Short video compose: the timeline names "
                f"{len(missing)} artifact(s) that no incoming item carries — {', '.join(missing)}. "
                "Every clip and the narration must reach this step as an attachment."
            )
        return found, named

    async def _normalise(
        self, ctx: ExecutionContext, ref: BinaryRef, *, track: str, target_lufs: float
    ) -> BinaryRef:
        """One audio track, brought to §7's integrated-loudness target by the
        `shortvideo-audio` backend. The renderer cannot measure integrated
        loudness, so this is where the frozen numbers are actually applied —
        and a failure here fails the step rather than rendering at whatever
        level the provider happened to deliver."""
        result = await ctx.run_tool(
            ToolSpec(
                id=f"loudnorm:{ctx.node_id}:{track}",
                kind="curated",
                entrypoint=loudness.BACKEND_ID,
            ),
            {
                "backend": loudness.BACKEND_ID,
                "preset": loudness.PRESET_NAME,
                # §7 states one true-peak ceiling, for narration; a music bed
                # gets the same one, since nothing is gained by letting the
                # bed peak higher than the voice over it.
                "params": {
                    "target_lufs": target_lufs,
                    "true_peak_dbtp": NARRATION_TRUE_PEAK_DBTP,
                },
                "inputs": [ref.model_dump(mode="json")],
            },
        )
        if not result.ok:
            raise RuntimeError(
                f"Short video compose could not normalise the {track} loudness: {result.error}"
            )
        normalised = (result.binary or {}).get("audio")
        if normalised is None:
            raise RuntimeError(
                f"Short video compose: loudness normalisation of the {track} produced no audio"
            )
        return normalised

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        document = self._timeline_document(ctx)

        # 1. Validate the authoring form. Nothing has been fetched yet, and
        #    nothing will be if this raises.
        try:
            timeline = timeline_from_json(document)
            validate(timeline)
        except TimelineError as error:
            raise NodeConfigurationError(f"Short video compose: {error}") from error

        digest = timeline_digest(timeline)
        expected = str(ctx.param("expected_digest") or "").strip()
        if expected and expected != digest:
            raise NodeConfigurationError(
                "Short video compose: this timeline is not the one that was approved "
                f"(digest {digest[:12]}…, approved {expected[:12]}…). Nothing was rendered."
            )

        # 2. Resolve every ref, then fix the order the runtime materializes in.
        #    The timeline document goes first so the preset can name it without
        #    counting; media follows in a stable order the render document
        #    mirrors.
        ordered_ids = [timeline.narration.ref_id]
        if timeline.music is not None:
            ordered_ids.append(timeline.music.ref_id)
        ordered_ids.extend(beat.clip.ref_id for beat in timeline.beats)
        refs, sources = self._resolve_refs(ctx, set(ordered_ids))

        # 2b. Apply §7's integrated-loudness targets to every audio track.
        #     After validation and ref resolution — so a bad timeline still
        #     costs nothing — and before the render, which can honour relative
        #     levels but cannot measure an absolute one.
        refs[timeline.narration.ref_id] = await self._normalise(
            ctx,
            refs[timeline.narration.ref_id],
            track="narration",
            target_lufs=NARRATION_TARGET_LUFS,
        )
        if timeline.music is not None:
            refs[timeline.music.ref_id] = await self._normalise(
                ctx, refs[timeline.music.ref_id], track="music", target_lufs=MUSIC_TARGET_LUFS
            )

        # 3. Rename on the way in. The child never learns an original file name
        #    or a ref id — and the deterministic suffix is what lets the render
        #    document name the paths the runtime is about to create.
        media: list[BinaryRef] = []
        paths: dict[str, str] = {}
        names = {timeline.narration.ref_id: "narration"}
        if timeline.music is not None:
            names[timeline.music.ref_id] = "music"
        for beat in timeline.beats:
            names[beat.clip.ref_id] = f"clip-{beat.index:03d}"
        for ref_id in ordered_ids:
            ref = refs[ref_id]
            suffix = (ref.file_name or "").rpartition(".")[2].lower()
            if not (suffix.isalnum() and 1 <= len(suffix) <= 5):
                suffix = (ref.mime_type or "").partition("/")[2].split(";")[0].strip() or "bin"
            renamed = ref.model_copy(update={"file_name": f"{names[ref_id]}.{suffix}"})
            # +1: the timeline document itself occupies index 0.
            paths[ref_id] = materialized_name(len(media) + 1, renamed)
            media.append(renamed)

        # 4. Build the render document — the authoring form with every ref
        #    replaced by the path it will be materialized to.
        try:
            render_doc = render_document(timeline, paths)
        except TimelineError as error:  # pragma: no cover - paths come from the same refs
            raise NodeConfigurationError(f"Short video compose: {error}") from error

        timeline_ref = await ctx.put_binary(
            json.dumps(render_doc, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            "application/json",
            "timeline.json",
        )

        draft = bool(ctx.param("draft"))
        tool_input: dict[str, Any] = {
            "backend": BACKEND_ID,
            "preset": PRESET_NAME,
            "params": {"scale": 0.5 if draft else 1.0, "concurrency": 1},
            # Order is the contract with `ComposePreset`: document first.
            "inputs": [ref.model_dump(mode="json") for ref in [timeline_ref, *media]],
        }

        async with self._gate.slot(ctx.workspace_id):
            result = await ctx.run_tool(
                ToolSpec(id=f"compose:{ctx.node_id}", kind="curated", entrypoint=BACKEND_ID),
                tool_input,
            )

        if not result.ok:
            raise RuntimeError(f"Short video compose failed: {_last_line(result.error)}")
        rendered = (result.binary or {}).get("video")
        if rendered is None:
            raise RuntimeError("Short video compose: the render produced no output attachment")

        # After success and outside the gate: a slow report must not hold a
        # render slot, and a failed one must not fail a finished video.
        usage = await report_render(ctx)

        attachment = str(ctx.param("attachment") or "video").strip() or "video"
        # The sources travel on with the render — the originals, not the
        # loudness-normalised copies, so a second compose of the same timeline
        # (the final after an approved draft) resolves the same refs and
        # computes the same digest. That is what lets an approval gate sit
        # between two composes with nothing re-assembled in between.
        carried = {name: ref for name, ref in sources.items() if name != attachment}
        return {
            "main": [
                Item.model_validate(
                    {
                        "json": {
                            "digest": digest,
                            "template": timeline.template,
                            "width": round(timeline.width * (0.5 if draft else 1.0)),
                            "height": round(timeline.height * (0.5 if draft else 1.0)),
                            "fps": timeline.fps,
                            "frames": timeline.total_frames,
                            "duration_seconds": round(timeline.total_seconds, 3),
                            "scale": 0.5 if draft else 1.0,
                            "draft": draft,
                            "beats": len(timeline.beats),
                            "duration_ms": result.duration_ms,
                            # The declared working set, not a measurement of
                            # this render: `RLIMIT_AS` is off for a browser, so
                            # nothing here observed the real figure.
                            "declared_memory_mb": RENDER_MEMORY_MB,
                            # Whether Remotion was told about this render
                            # — `not_configured` / `reported` / `failed`.
                            "remotion_usage_report": usage["status"],
                            "remotion_usage_detail": usage["detail"],
                            "timeline": as_json(timeline),
                            # Which attachment is the video — the item also
                            # carries the timeline's sources, and a reviewer's
                            # preview must not pick a clip by mistake.
                            "attachment": attachment,
                        },
                        "binary": {**carried, attachment: rendered},
                    }
                )
            ]
        }
