# `tamtree-remotion-render`

The curated render executable behind `shortvideo.compose`. It takes one
**TimelineV1 render document** and produces one mp4, and it is the only part of
this plugin that is not Python.

It is **not** shipped in the Python wheel. It is a Node program that belongs to
the worker image: V3.4 installs it, bundles the compositions, and bakes in
Chrome Headless Shell and the caption font. A worker without that image reports
the tool as unavailable — `RemotionBackend.resolve_binary` raises, and the
curated runtime turns that into "not available on this worker" rather than a
render failure.

## What it is given, and what it refuses

```
tamtree-remotion-render \
  --timeline _in/0.json \     # the render document, workdir-relative
  --out output.mp4 \          # workdir root, collected by the runtime
  --bundle /opt/tamtree/remotion/bundle \      # absolute, pre-built at image build
  --browser /opt/tamtree/chrome/chrome-headless-shell \  # absolute, baked in
  --scale 1 \                 # 0.5 for the approval draft
  --concurrency 1
```

Every argument is a path or a bounded number compiled by `ComposePreset`.
**Nothing a workflow author writes reaches this command line** — caption text,
a topic, a provider's file name all ride inside the document, which is a file.
That is D1's rule, and the argv is where it is either kept or lost.

Four refusals are deliberate and each has a reason the V0.3 spike measured:

| Refusal | Why |
|---|---|
| a non-absolute `--browser` | Remotion resolves its cached browser against the CWD, and SEC-D1 hands the child a fresh empty workdir — so a relative path downloads 88 MB of Chrome **on every render** (142ms with the flag, 17,936ms without) and executes it |
| a missing `--bundle` or `--browser` | the image's half of the contract; failing here costs milliseconds, failing later costs a browser start |
| an unknown or repeated flag | a preset and an executable that drift apart must fail, not render with the old meaning |
| a document that disagrees with itself | frame math is re-derived, not trusted: a document whose beats do not sum to `total_frames` would otherwise render as a plausible wrong video |

## Why there is a web server in here

`src/media-server.mjs` serves the workdir over `http://127.0.0.1`, and the
render document's media paths are turned into URLs on it.

This is not a convenience. Remotion serves its bundle to Chrome over http, and
a page served over http **cannot load `file://` resources** — Chrome blocks it.
The bundle's own `public/` directory is baked at image-build time, so it cannot
hold per-invocation media either. Loopback is the only route, and it only
exists because SEC-D1 now brings `lo` up inside the sandbox's network namespace
(contracts 1.36.0). That namespace has no address, route or interface to
anything outside it: this server is reachable by this render and by nothing
else.

The server is GET-only, one directory, no listing, and every path is resolved
with `realpath` and checked to be inside the workdir before a byte is read.

## Layout

```
bin/tamtree-remotion-render.mjs   the executable; decides what an error looks like
src/args.mjs                      strict argv parsing (no Remotion import — testable)
src/document.mjs                  the render document, re-validated at the sandbox edge
src/media-server.mjs              loopback file server for the workdir
src/render.mjs                    selectComposition + renderMedia
src/Root.tsx                      the two compositions, sized from the document
src/Short.tsx                     beats, trim/pad, crossfades, the audio mix
src/Captions.tsx                  the caption band, inside the frozen safe area
scripts/bundle.mjs                build the static site (image build time, never per render)
```

## Tests

```
npm test          # node:test, no dependencies — runs in a checkout with no node_modules
npm run typecheck # needs `npm ci`
```

`npm test` covers argument parsing, document validation and the media server —
everything that decides whether a render is the *right* one, all of which
happens before Chrome exists. It deliberately does not assert on a rendered
video: that needs the image, a browser and a golden frame, and it is V3.4/V3.5's
to prove.

## What this renderer does not do

**Integrated loudness normalization.** The document carries `target_lufs` and
`true_peak_ceiling_dbtp`, and they describe normalization applied to the
narration **upstream** — a browser cannot measure integrated loudness, and a
renderer that pretended to would produce a number nobody could trust. What it
does honour is every *relative* level: the clip-audio policy (`mute` / `duck` /
`keep`) and the music duck.

**Sidechain ducking.** v1's narration runs the length of the video, so "ducked
whenever narration plays" and "ducked throughout" are the same mix. The attack
and release times ride in the document for the version that does measure, and
are deliberately unused rather than approximated into something that only looks
like ducking.
