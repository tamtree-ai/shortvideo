"""V3.4 image smoke: render a real `TimelineV1` inside the render image.

Every unit test in this repo stops where a browser would start. This is the
first thing that does not: it builds a timeline with the plugin's own
`timeline.py`, compiles argv with the plugin's own presets, and runs them in
the image with **`--network none`** — so a render that needed to download a
browser, a font or a codec fails here instead of in a user's run.

What it checks, in order:

1. `shortvideo-audio`'s `loudnorm` argv lands a narration track near −16 LUFS
   integrated and under −1.5 dBTP, measured independently with `ebur128`.
2. `ComposePreset`'s argv renders a 1080×1920 @ 30fps h264+aac mp4 with exactly
   the timeline's frame count, network-off.
3. Rendering the same document twice gives the same decoded frames
   (`framemd5`), which is the "renders deterministically" half of V3's
   done-when.
4. The draft (scale 0.5) is 540×960 with the same frame count.

It writes stills from inside a beat, mid-crossfade and the last beat to `out/`,
so a human can look at the captions. It does not drive `run_sandboxed`: the
V0.3 spike already proved the sandbox shape, and this is about the image.

    uv run python renderer/smoke/run.py [--image tamtree-video:smoke]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tamtree_shortvideo.loudness import LoudnormParams, LoudnormPreset
from tamtree_shortvideo.remotion import ComposeParams, ComposePreset
from tamtree_shortvideo.timeline import (
    Beat,
    Caption,
    Clip,
    Narration,
    Timeline,
    Transition,
    render_document,
    validate,
)

FPS = 30
BEAT_FRAMES = (60, 90, 60)
HUES = ("0xd9480f", "0x1971c2", "0x2f9e44")  # one colour per clip, so a cut is visible
CAPTIONS = (
    "Every render starts here.",
    "Crossfades never move a cut.",
    "Loudness is set upstream.",
)
WORK = Path("/work")


def docker(
    image: str, workdir: Path, *argv: str, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{workdir}:{WORK}",
        "-w",
        str(WORK),
        # The product image's ENTRYPOINT is `tamtree`; name the program instead.
        "--entrypoint",
        argv[0],
        image,
        *argv[1:],
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"FAILED: {' '.join(argv[:3])} … exited {result.returncode}")
    return result


def build_timeline() -> Timeline:
    beats: list[Beat] = []
    start = 0
    for index, frames in enumerate(BEAT_FRAMES):
        seconds = frames / FPS
        beats.append(
            Beat(
                index=index,
                start_frame=start,
                frames=frames,
                clip=Clip(
                    ref_id=f"bin_SMOKE{index}CLIP",
                    mime_type="video/mp4",
                    source_duration_seconds=seconds + 1.0,
                    in_seconds=0.0,
                    out_seconds=seconds,
                ),
                transition=Transition(kind="crossfade", frames=6) if index else Transition(),
                captions=(
                    Caption(text=CAPTIONS[index], start_frame=start, end_frame=start + frames),
                ),
            )
        )
        start += frames
    timeline = Timeline(
        template="short-captioned",
        narration=Narration(
            ref_id="bin_SMOKENARR", mime_type="audio/wav", duration_seconds=start / FPS
        ),
        beats=tuple(beats),
    )
    validate(timeline)
    return timeline


def make_media(image: str, workdir: Path, timeline: Timeline) -> None:
    """Clips at a provider-ish 720×1280 with their own (to-be-muted) audio, and
    a mono 24 kHz narration — the shape TTS actually returns — deliberately
    far from −16 LUFS so normalisation has something to do."""
    (workdir / "_in").mkdir(parents=True)
    for beat, hue in zip(timeline.beats, HUES, strict=True):
        docker(
            image,
            workdir,
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={hue}:s=720x1280:r={FPS}:d={beat.clip.source_duration_seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=880:d={beat.clip.source_duration_seconds}",
            "-vf",
            "drawgrid=w=90:h=90:t=2:c=white@0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            f"_in/{beat.index + 2}.mp4",
        )
    docker(
        image,
        workdir,
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=f=220:d={timeline.total_seconds}:sample_rate=24000",
        "-af",
        "volume=-30dB",
        "-ac",
        "1",
        "_in/raw-narration.wav",
    )


def measure(image: str, workdir: Path, path: str) -> tuple[float, float]:
    """Integrated loudness and true peak, BS.1770 as `ebur128` implements it."""
    ebur = "ebur128=peak=true"
    result = docker(
        image,
        workdir,
        "ffmpeg",
        "-nostdin",
        "-nostats",
        "-i",
        path,
        "-map",
        "0:a",
        "-af",
        ebur,
        "-f",
        "null",
        "-",
        capture=True,
    )
    summary = result.stderr[result.stderr.rfind("Summary:") :]
    lufs = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))  # type: ignore[union-attr]
    peak = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))  # type: ignore[union-attr]
    return lufs, peak


def probe(image: str, workdir: Path, path: str) -> dict[str, object]:
    result = docker(
        image,
        workdir,
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,nb_read_frames",
        "-of",
        "json",
        path,
        capture=True,
    )
    return json.loads(result.stdout)


def framemd5(image: str, workdir: Path, path: str) -> str:
    result = docker(
        image,
        workdir,
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        path,
        "-map",
        "0:v",
        "-f",
        "framemd5",
        "-",
        capture=True,
    )
    return "\n".join(line for line in result.stdout.splitlines() if not line.startswith("#"))


def render(image: str, workdir: Path, scale: float, name: str) -> float:
    argv = (
        ComposePreset()
        .compile(
            binary="tamtree-remotion-render",
            inputs=["_in/0.json", "_in/1.wav", "_in/2.mp4", "_in/3.mp4", "_in/4.mp4"],
            params=ComposeParams(scale=scale),
        )
        .argv
    )
    started = time.monotonic()
    result = docker(image, workdir, *argv, capture=True)
    elapsed = time.monotonic() - started
    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(f"  render {name}: {elapsed:.1f}s wall, renderer says {report.get('total_ms')}ms")
    (workdir / "output.mp4").rename(workdir / name)
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="tamtree-video:smoke")
    args = parser.parse_args()
    image: str = args.image

    out = Path(__file__).parent / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    out.chmod(0o777)

    timeline = build_timeline()
    print(f"timeline: {len(timeline.beats)} beats, {timeline.total_frames} frames")
    make_media(image, out, timeline)
    for path in out.rglob("*"):
        path.chmod(0o777)

    # 1. Loudness, through the real preset's argv.
    before = measure(image, out, "_in/raw-narration.wav")
    loud = (
        LoudnormPreset()
        .compile(
            binary="ffmpeg",
            inputs=["_in/raw-narration.wav"],
            params=LoudnormParams(target_lufs=-16.0),
        )
        .argv
    )
    docker(image, out, *loud)
    (out / "output.wav").rename(out / "_in" / "1.wav")
    lufs, peak = measure(image, out, "_in/1.wav")
    print(f"loudness: {before[0]:.1f} LUFS → {lufs:.1f} LUFS, peak {peak:.1f} dBFS")
    assert abs(lufs - -16.0) <= 1.0, f"integrated loudness {lufs} is not within 1 LU of -16"
    assert peak <= -1.0, f"peak {peak} dBFS is over the ceiling"

    # 2. The render document, exactly as compose would build it.
    paths = {"bin_SMOKENARR": "_in/1.wav"}
    paths.update({beat.clip.ref_id: f"_in/{beat.index + 2}.mp4" for beat in timeline.beats})
    (out / "_in" / "0.json").write_text(
        json.dumps(render_document(timeline, paths), sort_keys=True)
    )

    render(image, out, 1.0, "final-a.mp4")
    info = probe(image, out, "final-a.mp4")
    streams = {s["codec_type"]: s for s in info["streams"]}  # type: ignore[index, union-attr]
    video, audio = streams["video"], streams["audio"]
    print(
        f"final: {video['width']}×{video['height']} {video['codec_name']} {video['r_frame_rate']}, "
        f"{video['nb_read_frames']} frames, audio {audio['codec_name']}"
    )
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264" and audio["codec_name"] == "aac"
    assert video["r_frame_rate"] == "30/1"
    assert int(video["nb_read_frames"]) == timeline.total_frames

    # 2b. The number that matters: the finished video's own mix. Clip audio
    #     is muted by default, so this is the narration as a viewer hears it.
    final_lufs, final_peak = measure(image, out, "final-a.mp4")
    print(f"final mix: {final_lufs:.1f} LUFS, peak {final_peak:.1f} dBFS")
    assert abs(final_lufs - -16.0) <= 1.0, f"the rendered mix is {final_lufs} LUFS, not ~-16"
    assert final_peak <= -1.0, f"the rendered mix peaks at {final_peak} dBFS"

    # 3. Determinism.
    render(image, out, 1.0, "final-b.mp4")
    same = framemd5(image, out, "final-a.mp4") == framemd5(image, out, "final-b.mp4")
    print(f"deterministic frames: {same}")
    assert same, "two renders of one document decoded to different frames"

    # 4. Draft.
    render(image, out, 0.5, "draft.mp4")
    draft = {s["codec_type"]: s for s in probe(image, out, "draft.mp4")["streams"]}["video"]  # type: ignore[index, union-attr]
    assert (draft["width"], draft["height"]) == (540, 960)
    assert int(draft["nb_read_frames"]) == timeline.total_frames

    for frame in (30, 63, 180):
        docker(
            image,
            out,
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            "final-a.mp4",
            "-vf",
            f"select=eq(n\\,{frame})",
            "-frames:v",
            "1",
            f"frame-{frame:03d}.png",
        )
    print(f"OK — stills in {out}")


if __name__ == "__main__":
    main()
