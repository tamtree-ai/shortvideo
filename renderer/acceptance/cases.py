"""V3.5 acceptance: the render's negative and resource cases, through the real runtime.

`renderer/smoke/run.py` proves the image renders. This proves what happens
when the render is attacked or runs out of something — and it does so through
the product's own `CuratedCliToolRuntime` and `run_sandboxed`, not an imitation:
the SEC-D3 gate, input materialization, the SEC-D1 sandbox and output
collection are the shipped code. `ComposeNode` drives it where the claim is
about the node; a probe backend with the renderer's exact limits stands in
where the claim is about what the render child can see or reach.

It runs **inside** the acceptance image (`run.sh` builds it), as root with
`CAP_SYS_ADMIN` and the container's network left **on** — so the only thing
between the render child and the internet is the netns SEC-D1 creates. A
harness run with `--network none` would pass the no-network case whether or not
the sandbox worked.

Each case prints one line, `PASS name — detail` or `FAIL name — detail`, and the
process exits non-zero if any case failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from tamtree_nodes.curated import CuratedCliToolRuntime
from tamtree_nodes.sandbox import netns_isolation_available
from tamtree_plugin_sdk import BinaryRef, CompiledCommand, Item, PresetOutput, ToolSpec
from tamtree_plugin_sdk.testing import FakeExecutionContext
from tamtree_sdk.testing.fakes import FakeBinaryStore

from tamtree_shortvideo.compose import ComposeNode
from tamtree_shortvideo.loudness import LoudnessBackend
from tamtree_shortvideo.remotion import RemotionBackend, render_limits
from tamtree_shortvideo.timeline import (
    Beat,
    Caption,
    Clip,
    Narration,
    Timeline,
    Transition,
    as_json,
    validate,
)

FPS = 30
MEDIA = Path(tempfile.mkdtemp(prefix="acceptance-"))
RESULTS: list[tuple[bool, str, str]] = []

#: Captions a hostile or merely careless upstream could produce. Each must
#: arrive in the frame as text: React escapes it, and none of it is argv.
HOSTILE_CAPTIONS = (
    "<img src=x onerror=\"fetch('http://1.1.1.1/x')\">",
    "</div><script>document.title='pwned'</script>",
    "--browser /tmp/evil $(id) `id` ; rm -rf / && echo",
)


# --- fixtures ----------------------------------------------------------------


def ffmpeg(*argv: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *argv], check=True)


def make_clip(name: str, seconds: float, *, size: str = "720x1280", noisy: bool = False) -> Path:
    """`noisy` fills every frame with temporal noise, which h264 cannot
    compress — the way to get a render whose *output* is large."""
    path = MEDIA / name
    video = f"color=c=0x1971c2:s={size}:r={FPS}:d={seconds}"
    if noisy:
        video += ",noise=alls=100:allf=t"
    ffmpeg(
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", f"sine=f=880:d={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def make_narration(seconds: float) -> Path:
    path = MEDIA / "narration.wav"
    ffmpeg(
        "-f", "lavfi", "-i", f"sine=f=220:d={seconds}:sample_rate=24000",
        "-af", "volume=-24dB", "-ac", "1", str(path),
    )  # fmt: skip
    return path


def fake_ctx(rt: Any) -> FakeExecutionContext:
    """The SDK fake, with the one change a real runtime needs: it hands
    `run_tool` a put-only store, and `CuratedCliToolRuntime` refuses anything
    that cannot also *read* an input (it has to materialize them). Production's
    `CtxBinaryStore` does both; `FakeBinaryStore` over the same bytes is the
    fake-side equivalent. (An SDK testkit gap — recorded in the handover.)"""
    ctx = FakeExecutionContext(params={}, tool_runtime=rt)
    ctx._binary_putter = FakeBinaryStore(  # noqa: SLF001
        workspace_id=ctx.workspace_id,
        binaries=ctx._binaries,  # noqa: SLF001
    )
    return ctx


def runtime(**overrides: Any) -> CuratedCliToolRuntime:
    """Both of this plugin's backends, the renderer's limits optionally
    overridden — the way a deployment's env vars would."""
    limits = dataclasses.replace(render_limits(), **overrides)
    return CuratedCliToolRuntime(
        {"remotion": RemotionBackend(limits), "shortvideo-audio": LoudnessBackend()}
    )


async def compose_ctx(
    rt: Any,
    *,
    beat_frames: tuple[int, ...] = (45, 45),
    captions: tuple[str, ...] | None = None,
    clip_bytes: bytes | None = None,
    clip_size: str = "720x1280",
    noisy: bool = False,
) -> FakeExecutionContext:
    """A context whose store holds real media and whose one incoming item
    carries a timeline naming it — what `shortvideo.compose` meets in a run."""
    ctx = fake_ctx(rt)
    total = sum(beat_frames) / FPS
    narration = await ctx.put_binary(make_narration(total).read_bytes(), "audio/wav", "tts.wav")
    binary: dict[str, BinaryRef] = {narration.id: narration}
    beats: list[Beat] = []
    start = 0
    for index, frames in enumerate(beat_frames):
        seconds = frames / FPS
        data = (
            clip_bytes
            or make_clip(
                f"clip{index}.mp4", seconds + 1.0, size=clip_size, noisy=noisy
            ).read_bytes()
        )
        ref = await ctx.put_binary(data, "video/mp4", f"provider-{index}.mp4")
        binary[ref.id] = ref
        text = (captions or ("Beat.",) * len(beat_frames))[index]
        beats.append(
            Beat(
                index=index,
                start_frame=start,
                frames=frames,
                clip=Clip(ref.id, "video/mp4", seconds + 1.0, 0.0, seconds),
                transition=Transition("crossfade", 6) if index else Transition(),
                captions=(Caption(text, start, start + frames),),
            )
        )
        start += frames
    timeline = Timeline(
        template="short-captioned",
        narration=Narration(narration.id, "audio/wav", total),
        beats=tuple(beats),
    )
    validate(timeline)
    ctx._inputs = {  # noqa: SLF001 — a harness seeding the fake's inputs
        "main": [Item.model_validate({"json": {"timeline": as_json(timeline)}, "binary": binary})]
    }
    return ctx


def census() -> list[str]:
    """Every live process that belongs to a render. After a render ends — by
    success, timeout or cancel — this must be empty: an orphaned Chrome is a
    leak per render, and on a worker that is a slow OOM."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if any(
            mark in cmdline
            for mark in ("chrome-headless-shell", "tamtree-remotion-render", "remotion/compositor")
        ):
            found.append(f"{pid}: {cmdline[:120]}")
    return found


# --- a probe that sees exactly what a render sees -----------------------------


class _ProbeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


PROBE_SCRIPT = r"""
const fs = require('node:fs');
const net = require('node:net');
const dns = require('node:dns').promises;
const out = {};
const limits = fs.readFileSync('/proc/self/limits', 'utf8');
for (const line of limits.split('\n').slice(1)) {
  const m = line.match(/^(Max [a-z ]+?)\s{2,}(\S+)\s+(\S+)/);
  if (m) out[m[1]] = {soft: m[2], hard: m[3]};
}
out.uid = process.getuid();
const attempt = (fn) => fn().then(() => 'reached', (e) => `refused: ${e.code || e.message}`);
const connect = (host, port) => () => new Promise((resolve, reject) => {
  const s = net.connect({host, port, timeout: 3000});
  s.on('connect', () => { s.destroy(); resolve(); });
  s.on('timeout', () => { s.destroy(); reject(new Error('timeout')); });
  s.on('error', reject);
});
(async () => {
  out.dns = await attempt(() => dns.lookup('example.com'));
  out.egress_ip = await attempt(connect('1.1.1.1', 443));
  const signal = AbortSignal.timeout(3000);
  out.egress_http = await attempt(() => fetch('http://example.com', {signal}));
  const server = net.createServer((c) => c.end()).listen(0, '127.0.0.1');
  await new Promise((r) => server.on('listening', r));
  out.loopback = await attempt(connect('127.0.0.1', server.address().port));
  server.close();
  fs.writeFileSync('output.json', JSON.stringify(out));
})();
"""


class _ProbePreset:
    name: str = "probe"
    params_model: type[BaseModel] = _ProbeParams

    def compile(self, *, binary: str, inputs: Any, params: BaseModel) -> CompiledCommand:
        return CompiledCommand(
            argv=[binary, "-e", PROBE_SCRIPT],
            output=PresetOutput(
                attachment="report", rel_path="output.json", mime="application/json"
            ),
        )


class _ProbeBackend:
    """Node, under **the renderer's own limits** — so what it reports is what a
    render child is given, not what a generic curated tool is."""

    id: str = "render-probe"

    def __init__(self) -> None:
        self.limits = render_limits()
        self.presets = {"probe": _ProbePreset()}

    def resolve_binary(self) -> str:
        return "/usr/local/bin/node"


async def probe() -> dict[str, Any]:
    ctx = fake_ctx(CuratedCliToolRuntime({"render-probe": _ProbeBackend()}))
    dummy = await ctx.put_binary(b"{}", "application/json", "in.json")
    result = await ctx.run_tool(
        ToolSpec(id="probe", kind="curated", entrypoint="render-probe"),
        {"backend": "render-probe", "preset": "probe", "inputs": [dummy.model_dump(mode="json")]},
    )
    if not result.ok:
        raise AssertionError(f"probe failed: {result.error}")
    return json.loads(await ctx.get_binary(result.binary["report"]))


# --- the cases ---------------------------------------------------------------


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} — {detail}", flush=True)


def case(fn: Callable[[], Awaitable[str]]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        started = time.monotonic()
        try:
            detail = await fn()
            record(True, fn.__name__, f"{detail} ({time.monotonic() - started:.1f}s)")
        except Exception as error:  # noqa: BLE001 — a harness reports, it does not crash
            tb = traceback.format_exc(limit=3).strip().splitlines()[-1]
            record(False, fn.__name__, f"{error!s:.400} [{tb}]")
        await asyncio.sleep(0.5)  # let a killed group finish exiting
        leftovers = census()
        if leftovers:
            record(False, f"{fn.__name__}:cleanup", "orphans: " + "; ".join(leftovers))

    return run


#: Failures that mean the *environment* broke, not the case under test. A
#: negative case that fails with one of these has proved nothing about its
#: own claim — which is exactly how the first run of this harness produced
#: three false passes.
WRONG_REASONS = ("ENETUNREACH", "Failed to launch the browser")


async def refused(ctx: FakeExecutionContext, *, because: tuple[str, ...]) -> str:
    """Run compose, require a failure, and require it to be *this* failure."""
    try:
        await ComposeNode().execute(ctx)
    except RuntimeError as error:
        message = str(error)
        for wrong in WRONG_REASONS:
            assert wrong not in message, f"failed for the wrong reason: {message[:300]}"
        assert any(reason in message for reason in because), (
            f"expected one of {because}, got: {message[:300]}"
        )
        return f"refused: {message[:160]}"
    raise AssertionError("expected a refusal, got a render")


@case
async def the_sandbox_really_has_a_netns() -> str:
    """Every network claim below is void without this, so it goes first."""
    assert netns_isolation_available(), "no CAP_SYS_ADMIN: SEC-D1 cannot create a netns here"
    return "unshare(CLONE_NEWNET) available"


@case
async def hostile_captions_render_as_text_through_the_real_runtime() -> str:
    ctx = await compose_ctx(runtime(), beat_frames=(45, 45, 45), captions=HOSTILE_CAPTIONS)
    result = await ComposeNode().execute(ctx)
    out = result["main"][0]
    video = await ctx.get_binary((out.binary or {})["video"])
    assert len(video) > 10_000, "no real mp4 came back"
    assert out.json_["frames"] == 135
    assert out.json_["remotion_usage_report"] == "not_configured"
    (MEDIA / "hostile.mp4").write_bytes(video)
    return f"{len(video)} bytes, 135 frames, usage report: not_configured"


@case
async def the_render_child_cannot_reach_the_network_but_has_loopback() -> str:
    report = await probe()
    for key in ("dns", "egress_ip", "egress_http"):
        assert report[key].startswith("refused"), f"{key} {report[key]}"
    assert report["loopback"] == "reached", (
        f"loopback {report['loopback']} — the media server needs it"
    )
    return f"dns {report['dns']}; 1.1.1.1 {report['egress_ip']}; loopback reached"


@case
async def the_render_child_s_ceilings_are_the_declared_ones() -> str:
    report = await probe()
    limits = render_limits()
    fsize, cpu = report["Max file size"], report["Max cpu time"]
    assert fsize["soft"] == str(limits.fsize_bytes), f"RLIMIT_FSIZE {fsize}"
    assert cpu["soft"] == str(limits.timeout_s), f"RLIMIT_CPU {cpu}"
    assert report["Max address space"]["soft"] == "unlimited", "RLIMIT_AS should be declined"
    nofile, nproc = report["Max open files"], report["Max processes"]
    return (
        f"fsize {fsize['soft']}, cpu {cpu['soft']}s, AS unlimited (declared), "
        f"nofile {nofile['soft']}, nproc {nproc['soft']}, uid {report['uid']}"
    )


@case
async def an_output_above_the_size_cap_fails_cleanly() -> str:
    """Noise-filled clips, so the mp4 itself is what outgrows the cap — not a
    Chrome cache file on the way up, which would prove only that Chrome needs
    disk. 4 MB is above anything Chrome writes at startup and far below a
    noisy 1080×1920 render."""
    ctx = await compose_ctx(runtime(fsize_bytes=4_000_000), beat_frames=(90, 90), noisy=True)
    detail = await refused(ctx, because=("compose failed",))
    return detail


@case
async def a_render_past_its_cpu_budget_is_killed_with_its_whole_tree() -> str:
    ctx = await compose_ctx(runtime(timeout_s=3), beat_frames=(90, 90, 90))
    return await refused(ctx, because=("time limit", "SIGXCPU", "CPU time"))


@case
async def a_cancelled_render_leaves_no_process_behind() -> str:
    ctx = await compose_ctx(runtime(), beat_frames=(120, 120, 120))
    task = asyncio.create_task(ComposeNode().execute(ctx))
    deadline = time.monotonic() + 30
    while not any("chrome-headless-shell" in p for p in census()):
        if task.done() or time.monotonic() > deadline:
            raise AssertionError("the render never reached Chrome, so cancel proved nothing")
        await asyncio.sleep(0.2)
    running = len(census())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(1.0)
    return f"cancelled with {running} render processes live"


@case
async def a_clip_that_is_not_video_fails_cleanly() -> str:
    ctx = await compose_ctx(runtime(), clip_bytes=os.urandom(64_000))
    # The sentence an author sees has to name the problem, not a stack frame.
    return await refused(ctx, because=("Invalid data found", "moov atom"))


@case
async def an_8k_clip_is_survived_or_refused_never_hung() -> str:
    """The decompression-bomb shape: a small file that decodes large. v1 has no
    per-process memory cap on a render (RLIMIT_AS is declined), so the claim is
    the deployment-level one: under the container's cgroup limit the render
    either finishes or dies, and either way returns and leaves nothing behind."""
    ctx = await compose_ctx(runtime(), clip_size="7680x4320")
    try:
        result = await ComposeNode().execute(ctx)
        return f"rendered ({result['main'][0].json_['frames']} frames)"
    except RuntimeError as error:
        return f"refused: {str(error)[:160]}"


@case
async def a_document_naming_a_missing_file_fails_before_chrome() -> str:
    ctx = fake_ctx(runtime())
    document = json.loads(json.dumps(_minimal_render_document()))
    doc_ref = await ctx.put_binary(json.dumps(document).encode(), "application/json", "t.json")
    wav = await ctx.put_binary(make_narration(3.0).read_bytes(), "audio/wav", "n.wav")
    started = time.monotonic()
    result = await ctx.run_tool(
        ToolSpec(id="missing", kind="curated", entrypoint="remotion"),
        {
            "backend": "remotion",
            "preset": "compose",
            "params": {"scale": 1.0, "concurrency": 1},
            "inputs": [doc_ref.model_dump(mode="json"), wav.model_dump(mode="json")],
        },
    )
    elapsed = time.monotonic() - started
    assert not result.ok, "a document naming a file nobody materialized rendered anyway"
    assert "not materialized" in (result.error or ""), result.error
    assert elapsed < 5, f"took {elapsed:.1f}s — that is a browser start, not a stat"
    return f"refused in {elapsed:.2f}s: {result.error[:100]}"


def _minimal_render_document() -> dict[str, Any]:
    """One 90-frame beat whose clip was never sent."""
    from tamtree_shortvideo.timeline import render_document

    timeline = Timeline(
        template="short-captioned",
        narration=Narration("n", "audio/wav", 3.0),
        beats=(
            Beat(0, 0, 90, Clip("c", "video/mp4", 4.0, 0.0, 3.0), captions=(Caption("x", 0, 90),)),
        ),
    )
    validate(timeline)
    return render_document(timeline, {"n": "_in/1.wav", "c": "_in/9.mp4"})


@case
async def hosted_mode_refuses_before_reading_a_byte() -> str:
    ctx = await compose_ctx(runtime())
    reads = 0
    store = ctx._binary_putter  # noqa: SLF001 — the store the runtime reads inputs from
    original = store.get_binary

    async def counting(ref: BinaryRef) -> bytes:
        nonlocal reads
        reads += 1
        return await original(ref)

    store.get_binary = counting
    os.environ["TAMTREE_DEPLOYMENT_PROFILE"] = "hosted"
    try:
        await ComposeNode().execute(ctx)
    except RuntimeError as error:
        assert reads == 0, f"{reads} input(s) were read before the SEC-D3 refusal"
        return f"refused, 0 bytes read: {str(error)[:120]}"
    finally:
        os.environ.pop("TAMTREE_DEPLOYMENT_PROFILE", None)
    raise AssertionError("hosted mode rendered")


async def main() -> None:
    for run in (
        the_sandbox_really_has_a_netns,
        hostile_captions_render_as_text_through_the_real_runtime,
        the_render_child_cannot_reach_the_network_but_has_loopback,
        the_render_child_s_ceilings_are_the_declared_ones,
        an_output_above_the_size_cap_fails_cleanly,
        a_render_past_its_cpu_budget_is_killed_with_its_whole_tree,
        a_cancelled_render_leaves_no_process_behind,
        a_clip_that_is_not_video_fails_cleanly,
        an_8k_clip_is_survived_or_refused_never_hung,
        a_document_naming_a_missing_file_fails_before_chrome,
        hosted_mode_refuses_before_reading_a_byte,
    ):
        await run()
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
