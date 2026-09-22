"""V3.2 acceptance: the curated backend behind `shortvideo.compose`.

Two kinds of test, and the split is deliberate. `TestRemotionBackendContract`
is the SDK's own conformance suite — it checks the claims the *family* makes
(params validated before any argv exists, a fixed argv out, `compile` pure),
and it runs with no binary installed and no sandbox, which is what makes them
checkable in a unit test at all.

The rest are this backend's own claims, and every one of them is a line the
V0.3 spike proved matters: an absolute browser path, a declined address-space
cap with a stated replacement, a CPU-second timeout, and a timeline that rides
a file rather than argv.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tamtree_plugin_sdk import CuratedBackend, CuratedCompileError
from tamtree_plugin_sdk.testing import CuratedBackendContract

from tamtree_shortvideo import remotion
from tamtree_shortvideo.remotion import (
    BACKEND,
    BACKEND_ID,
    DEFAULT_BROWSER,
    DEFAULT_BUNDLE,
    PRESET_NAME,
    ComposeParams,
    ComposePreset,
    RemotionBackend,
    RendererUnavailable,
    render_limits,
)

_INPUTS = ["_in/0.json", "_in/1.wav", "_in/2.mp4", "_in/3.mp4"]


def _argv(**params: object) -> list[str]:
    preset = ComposePreset(bundle="/opt/bundle", browser="/opt/chrome/headless-shell")
    command = preset.compile(
        binary="/usr/local/bin/tamtree-remotion-render",
        inputs=_INPUTS,
        params=ComposeParams(**params),
    )
    return command.argv


class TestRemotionBackendContract(CuratedBackendContract):
    """The family's own conformance suite, run against this backend."""

    def make_backend(self) -> CuratedBackend:
        return RemotionBackend(bundle="/opt/bundle", browser="/opt/chrome/headless-shell")

    def sample_inputs(self, preset: str) -> list[str]:
        # A compose render needs the timeline document *and* at least one media
        # file; the default single input would skip the compile tests.
        return list(_INPUTS)


# --- the backend's own declarations ----------------------------------------


def test_the_backend_declines_the_address_space_cap_and_says_why() -> None:
    """The one resource control this backend switches off. `RLIMIT_AS` caps
    virtual address space, which Chrome reserves by the terabyte — under it the
    child died in 0.2s inside Node's startup, before any render began."""
    assert render_limits().limit_address_space is False
    # `MediaLimits` requires a backend that declines the cap to declare a
    # replacement ceiling, and the module docstring is where an operator reads
    # what it is. Asserting on prose is unusual; it is here because "switched
    # off a resource control and said nothing" is the exact failure that would
    # otherwise pass every other test in this file.
    assert "replacement memory ceiling" in (remotion.__doc__ or "")
    assert "880 MB" in (remotion.__doc__ or "")


def test_the_timeout_is_a_cpu_second_budget_not_a_wall_clock_one() -> None:
    """`timeout_s` becomes `RLIMIT_CPU`, which is spent across cores — the
    spike's 5.1s wall render burned 8.4 CPU-seconds. A wall-clock budget would
    kill honest renders on a busy worker, so this is sized well above the v1
    180s ceiling."""
    assert render_limits().timeout_s >= 180 * 3


def test_missing_renderer_raises_the_plugin_side_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin cannot raise `MissingRuntime` — it lives in `tamtree_nodes` —
    so resolution failure has its own type, which the runtime reports as the
    tool being unavailable on this worker."""
    monkeypatch.delenv("TAMTREE_REMOTION_BIN", raising=False)
    monkeypatch.setattr("tamtree_shortvideo.remotion.shutil.which", lambda _name: None)
    with pytest.raises(RendererUnavailable, match="tamtree-remotion-render"):
        RemotionBackend().resolve_binary()


def test_the_binary_can_be_pointed_somewhere_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAMTREE_REMOTION_BIN", "/custom/render")
    assert RemotionBackend().resolve_binary() == "/custom/render"


def test_the_module_level_backend_is_what_the_entry_point_resolves_to() -> None:
    """The registry keys this family on `id` and refuses an entry-point name
    that disagrees with it, so the two must match here as well as in
    `pyproject.toml`."""
    assert BACKEND.id == BACKEND_ID == "remotion"
    assert set(BACKEND.presets) == {PRESET_NAME}


# --- the argv, which is the whole security surface -------------------------


def test_the_timeline_rides_a_path_never_its_contents() -> None:
    """D1's rule, made checkable: the document is named by path, and no part of
    it — caption text above all — is ever a command-line token."""
    argv = _argv()
    assert "--timeline" in argv
    assert argv[argv.index("--timeline") + 1] == "_in/0.json"
    assert all(len(token) < 200 for token in argv)


def test_the_browser_is_absolute_and_explicit() -> None:
    """The single most consequential argument. Without it Remotion resolves its
    cached browser against the CWD — which SEC-D1 makes a fresh empty workdir —
    and downloads 88 MB of Chrome on every render (142ms with it, 17,936ms
    without)."""
    argv = _argv()
    browser = argv[argv.index("--browser") + 1]
    assert browser.startswith("/")
    bundle = argv[argv.index("--bundle") + 1]
    assert bundle.startswith("/")


def test_the_image_paths_are_deployment_choices_not_workflow_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAMTREE_REMOTION_BUNDLE", "/srv/bundle")
    monkeypatch.setenv("TAMTREE_REMOTION_BROWSER", "/srv/chrome")
    argv = (
        ComposePreset().compile(binary="/bin/render", inputs=_INPUTS, params=ComposeParams()).argv
    )
    assert argv[argv.index("--bundle") + 1] == "/srv/bundle"
    assert argv[argv.index("--browser") + 1] == "/srv/chrome"
    # …and the defaults are where the image actually puts them.
    assert DEFAULT_BUNDLE.startswith("/") and DEFAULT_BROWSER.startswith("/")


def test_the_draft_scale_is_the_only_thing_a_draft_changes() -> None:
    """V4.3 rests on this: approving the draft and rendering the final must be
    the same decision, so scale may differ and nothing else may."""
    final = _argv(scale=1.0)
    draft = _argv(scale=0.5)
    assert draft[draft.index("--scale") + 1] == "0.5"
    assert final[final.index("--scale") + 1] == "1"
    del final[final.index("--scale") + 1], draft[draft.index("--scale") + 1]
    assert final == draft


def test_a_scale_outside_the_range_is_refused_before_any_argv_exists() -> None:
    for bad in (0.0, -1.0, 1.5):
        with pytest.raises(ValidationError):
            ComposeParams(scale=bad)


def test_concurrency_is_bounded_because_the_memory_cap_is_off() -> None:
    """Raising in-render concurrency multiplies the memory of a render that has
    no `RLIMIT_AS` ceiling, so the knob exists but cannot be opened far."""
    with pytest.raises(ValidationError):
        ComposeParams(concurrency=32)
    assert ComposeParams().concurrency == 1


def test_an_unknown_knob_is_a_refusal_not_a_silent_drop() -> None:
    with pytest.raises(ValidationError):
        ComposeParams(**{"chromium_flags": "--no-sandbox"})


def test_compile_needs_the_document_and_at_least_one_media_file() -> None:
    preset = ComposePreset()
    with pytest.raises(CuratedCompileError):
        preset.compile(binary="/bin/render", inputs=[], params=ComposeParams())
    with pytest.raises(CuratedCompileError, match="media"):
        preset.compile(binary="/bin/render", inputs=["_in/0.json"], params=ComposeParams())


def test_the_output_is_one_mp4_at_the_workdir_root() -> None:
    """The sandbox creates no subdirectory for output, so a preset that asked
    for one would collect nothing."""
    command = ComposePreset().compile(binary="/bin/render", inputs=_INPUTS, params=ComposeParams())
    assert command.output.rel_path == "output.mp4"
    assert "/" not in command.output.rel_path
    assert command.output.attachment == "video"
    assert command.output.mime == "video/mp4"
