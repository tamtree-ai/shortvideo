"""`RemotionBackend` — the curated backend behind `shortvideo.compose` (V3.2).

This is the whole of what this distribution contributes to the curated-tool
family: where to find `tamtree-remotion-render`, one operation (`compose`), and
the resource ceilings a browser render needs. Isolation, the SEC-D3 hosted
gate, input materialization and output collection are `CuratedCliToolRuntime`'s
and are not reachable from here — which is the point of D11 and the reason this
file can live in a published plugin at all.

**Three numbers here are not the obvious ones, and each was measured rather
than guessed** (V0.3 spike, 2026-09-20, `spike-v03/`):

- **`limit_address_space=False`.** `RLIMIT_AS` caps *virtual* address space.
  Chrome and V8 reserve ~6 TB of it against ~890 MB of RSS, so under the
  shipped curated parameters the child died in 0.2s inside Node's own startup
  (`WebAssembly.instantiate(): Out of memory`) — not during a render, before
  one. Declining the cap is not a request for more memory; the limit was never
  measuring memory for this tool. The replacement ceiling is declared below.
- **`timeout_s` is a CPU-second budget, not a wall-clock one.** It becomes
  `RLIMIT_CPU`, which is spent across cores: the spike's 5.1s wall render
  burned 8.4 CPU-seconds. A backend that budgeted wall-time would kill its own
  renders on a busy worker.
- **`fsize_bytes` bounds one file, not the render.** A 1080×1920 @ 30fps
  h264 short is ~1.9 MB for 5 seconds; the cap is far above any honest output
  and is there to stop a decompression bomb, not to size a video.

**The replacement memory ceiling, stated as `MediaLimits` requires.** With
`RLIMIT_AS` declined there is no per-process kernel cap on this render, and v1
does not pretend otherwise: the ceiling is *deployment-level*. Concurrency is
pinned at one render per invocation and the per-workspace gate caps concurrent
renders per **worker process** (not per deployment — see `compose.py`), and the
measured figure an operator sizes a worker with is **~880 MB of RSS and ~1.7
cores per concurrent render**. A cgroup memory limit on the worker container is
the enforcement; this backend cannot install one and says so rather than
implying a cap it does not have.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from tamtree_plugin_sdk import (
    CompiledCommand,
    CuratedCompileError,
    MediaLimits,
    Preset,
    PresetOutput,
)

__all__ = [
    "BACKEND_ID",
    "DEFAULT_BROWSER",
    "DEFAULT_BUNDLE",
    "PRESET_NAME",
    "RENDER_MEMORY_MB",
    "ComposeParams",
    "ComposePreset",
    "RemotionBackend",
    "RendererUnavailable",
]

#: The id that rides `ToolSpec.entrypoint`, and the name every error uses.
BACKEND_ID: Final = "remotion"
PRESET_NAME: Final = "compose"

#: Where the image puts the three things a render needs. All three are
#: overridable because a self-hoster may lay the image out differently; none of
#: them is a *workflow* input, because a render that could be pointed at an
#: arbitrary executable would not be a curated tool.
_BIN_ENV: Final = "TAMTREE_REMOTION_BIN"
_BUNDLE_ENV: Final = "TAMTREE_REMOTION_BUNDLE"
_BROWSER_ENV: Final = "TAMTREE_REMOTION_BROWSER"

DEFAULT_BUNDLE: Final = "/opt/tamtree/remotion/bundle"
DEFAULT_BROWSER: Final = "/opt/tamtree/chrome/chrome-headless-shell"

#: CPU-seconds, not wall-seconds (see the module docstring). Sized off the
#: spike's 8.4 CPU-seconds for a 5s render: ~1.7 CPU-seconds per second of
#: video, times the 180s v1 ceiling, times three for a cold cache and a slower
#: worker. A render that exceeds it is not slow, it is stuck.
_DEFAULT_TIMEOUT_S: Final = 900

#: The declared working set, in the absence of an `RLIMIT_AS` ceiling. Reported
#: to the sandbox and used by nothing there — kept because it is the number an
#: operator sizes a worker by, and a figure nobody writes down is a figure
#: nobody can check against a deployment.
RENDER_MEMORY_MB: Final = 1024

#: One mp4. The v1 ceiling is 180s of 1080×1920 h264, which lands well under
#: 200 MB; 512 MB is the bomb guard, not the budget.
_DEFAULT_FSIZE: Final = 512 * 1024 * 1024

_TIMEOUT_ENV: Final = "TAMTREE_REMOTION_TIMEOUT_S"
_FSIZE_ENV: Final = "TAMTREE_REMOTION_FSIZE"


class RendererUnavailable(RuntimeError):
    """`tamtree-remotion-render` is not installed on this worker.

    A plugin cannot raise `MissingRuntime` — it lives in `tamtree_nodes`, which
    a published distribution must not import — so this is the plugin-side
    spelling, and `CuratedCliToolRuntime` reports any resolution failure as the
    tool being unavailable here rather than as a render failure."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default


def render_limits() -> MediaLimits:
    return MediaLimits(
        fsize_bytes=_env_int(_FSIZE_ENV, _DEFAULT_FSIZE),
        timeout_s=_env_int(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S),
        memory_mb=_env_int("TAMTREE_REMOTION_MEMORY_MB", RENDER_MEMORY_MB),
        # The one control this backend declines, and the reason it can render
        # at all. See the module docstring for the replacement ceiling.
        limit_address_space=False,
    )


class ComposeParams(BaseModel):
    """The whole knob surface of a render, and it is deliberately tiny.

    Everything that varies about *what* is rendered is in the timeline
    document, which rides a materialized file. What is left here is how the
    render is performed, and every field is a bounded number or a fixed path
    chosen by the deployment — nothing a workflow author writes reaches argv.

    `extra="forbid"` is load-bearing rather than tidy: an unknown knob must be
    a refusal, because the alternative is a param that looks accepted and is
    silently dropped."""

    model_config = ConfigDict(extra="forbid")

    #: 1.0 is the final render; the draft in front of the approval gate is 0.5
    #: (540×960). Scale is the *only* permitted difference between the two —
    #: it changes no frame boundary, which is what makes approving the draft
    #: and rendering the final the same decision (V4.3).
    scale: float = Field(default=1.0, gt=0.0, le=1.0)
    #: Frames rendered in parallel *inside* one render. Pinned low on purpose:
    #: the spike measured ~880 MB and ~1.7 cores at the default, and the
    #: per-workspace gate is what provides fairness between renders. Raising
    #: this multiplies the memory of a render that has no `RLIMIT_AS` ceiling.
    concurrency: int = Field(default=1, ge=1, le=4)
    #: A node may ask for less time than the platform allows, never more; the
    #: runtime takes `min(this, limits.timeout_s)`. CPU-seconds, as above.
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    #: mp4/h264/aac is what both v1 templates declare. Present as a field
    #: rather than a literal so `TimelineV2` need not exist for a webm variant,
    #: and typed as an enum so it can only ever be one of two argv tokens.
    container: Literal["mp4"] = "mp4"


class ComposePreset:
    """Render one `TimelineV1` document to one mp4.

    The security claim of the curated family is that a preset turns validated
    params into a **fixed argv**, and this one is about as literal an example as
    the family has: the argv is eight flag/value pairs, six of them chosen by
    the deployment or by a bounded number, and the seventh — the document — is
    a *path* to a file the runtime materialized. Caption text, a topic, a
    filename from a provider: none of it is a command-line token, ever. That is
    D1's "a workflow cannot hand Remotion TSX, and captions cannot ride argv",
    made structural rather than remembered.
    """

    # Plain class attributes (not ClassVar) so this structurally satisfies the
    # `Preset` protocol's instance-variable members — mypy --strict refuses a
    # ClassVar where a Protocol declares an instance one.
    name: str = PRESET_NAME
    params_model: type[BaseModel] = ComposeParams

    def __init__(self, *, bundle: str | None = None, browser: str | None = None) -> None:
        #: Resolved once, at construction, so `compile` stays pure — two
        #: compiles of the same params must produce the same argv, and reading
        #: the environment inside `compile` would make that depend on when it
        #: ran.
        self._bundle = bundle or os.environ.get(_BUNDLE_ENV) or DEFAULT_BUNDLE
        self._browser = browser or os.environ.get(_BROWSER_ENV) or DEFAULT_BROWSER

    def compile(self, *, binary: str, inputs: Sequence[str], params: BaseModel) -> CompiledCommand:
        if not isinstance(params, ComposeParams):  # pragma: no cover - runtime validates
            raise CuratedCompileError("compose params were not validated")
        if not inputs:
            raise CuratedCompileError(
                "a compose render needs the timeline document as its first input"
            )

        # **Input order is the contract with the node.** The runtime
        # materializes `inputs` to `_in/0`, `_in/1`, … in the order it is
        # given, and `shortvideo.compose` puts the timeline document first and
        # the media after it, in the order the document names them. The
        # document refers to those media by the same workdir-relative paths, so
        # a mismatch is a missing file the renderer names — never a silently
        # swapped clip.
        timeline, *media = inputs
        if not media:
            raise CuratedCompileError(
                "a compose render needs at least one media input beside the timeline"
            )

        out_rel = f"output.{params.container}"
        argv = [
            binary,
            "--timeline",
            timeline,
            "--out",
            out_rel,
            "--bundle",
            self._bundle,
            # Absolute, and the single most consequential argument here: without
            # it Remotion resolves its cached browser against the CWD — which
            # SEC-D1 makes a fresh, empty, per-invocation directory — and
            # downloads 88 MB of Chrome over the network on *every* render
            # (V0.3 finding 4: 142ms with it, 17,936ms without).
            "--browser",
            self._browser,
            "--scale",
            f"{params.scale:g}",
            "--concurrency",
            str(params.concurrency),
        ]
        return CompiledCommand(
            argv=argv,
            output=PresetOutput(attachment="video", rel_path=out_rel, mime="video/mp4"),
        )


class RemotionBackend:
    """The `CuratedBackend` for the short-form renderer. `id` matches
    `ToolSpec.entrypoint`, and is what every error names."""

    id: str = BACKEND_ID  # plain attribute, so the Protocol matches structurally

    def __init__(
        self,
        limits: MediaLimits | None = None,
        *,
        bundle: str | None = None,
        browser: str | None = None,
    ) -> None:
        self.limits: MediaLimits = limits or render_limits()
        self.presets: Mapping[str, Preset] = {
            PRESET_NAME: ComposePreset(bundle=bundle, browser=browser)
        }

    def resolve_binary(self) -> str:
        binary = os.environ.get(_BIN_ENV) or shutil.which("tamtree-remotion-render")
        if not binary:
            raise RendererUnavailable(
                "Short video compose needs the tamtree-remotion-render executable on the "
                "worker — it ships in the video render image. Install that image, or set "
                f"{_BIN_ENV} to the executable's path."
            )
        return binary


#: The instance the `tamtree.curated_backends` entry point resolves to. One per
#: distribution: the registry keys it on `id`, refuses a second backend
#: claiming `remotion`, and refuses an entry-point name that disagrees with it.
BACKEND: Final = RemotionBackend()
