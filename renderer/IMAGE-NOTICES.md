# Notices for the short-video render image

The render image adds four kinds of third-party software to a Tamtree worker.
The npm packages are listed with their full licence texts in
`/opt/tamtree/remotion/THIRD_PARTY_NOTICES.md`, which is generated at build time
from the pruned production `node_modules`, and in a CycloneDX SBOM beside it
(`sbom.cdx.json`). This file covers the rest, plus the obligations a
self-hoster takes on by running the image.

## Remotion: a licence obligation on the operator

Remotion is **not** open-source software. It is distributed under the
[Remotion License](https://remotion.dev/license):

- It is free for an individual, or for an organisation or team of **up to three
  people**. Above that threshold, a **Company License** is required.
- The terms say that an end user who is given direct access to a Remotion
  codebase, and can view or modify it, is directly engaging with a Remotion
  project. This image contains the pre-built bundle, which is the compiled
  template source, so **every organisation above the threshold that runs this image needs its
  own Remotion licence.** Tamtree's licence does not cover you.
- Software that renders programmatically (Remotion's "Automators" category) is
  billed per successful render, and each render has to be reported against the
  licence key. Set `TAMTREE_REMOTION_LICENSE_KEY` on the worker, or
  `free-license` if you qualify. The key is never baked into this image.
  Without it, renders still work and every render reports
  `remotion_usage_report: not_configured`.
- Remotion's terms name rendering arbitrary user-submitted Remotion videos on a
  server as an unacceptable use. v1 renders only its two shipped templates and
  refuses hosted multi-tenant mode (SEC-D3).

## Chrome Headless Shell

Installed at `/opt/tamtree/chrome/`. It is a build of Chromium, distributed
under the BSD-3-Clause licence and the third-party licences listed in the
Chromium source tree. The exact version is the one `@remotion/renderer` pins.
`browser.json` beside the executable records that version, the pinning package
and the executable's sha256.

## ffmpeg and codecs

The image contains ffmpeg twice, and the two are not the same build:

- **Debian's `ffmpeg` package** (LGPL-2.1+/GPL-2+, depending on how Debian
  configured it). `shortvideo.compose` uses it for loudness normalisation, and
  the product's `tamtree.media` node uses it too. Source and licence files come
  from Debian (`/usr/share/doc/ffmpeg*/copyright`).
- **Remotion's compositor binaries** (`@remotion/compositor-linux-*`), which
  include Remotion's own ffmpeg build. Remotion uses it to encode the final mp4.
  The npm package declares no licence field. The generated notices list it
  under "Packages declaring no licence" so a reviewer looks at it.

The render output is **H.264 video with AAC audio**. Both formats are covered by
patent pools (Via LA, formerly MPEG LA, for AVC/H.264 and for AAC). Whether
encoding and distributing them needs a patent licence depends on your
jurisdiction and your use. This image does not grant one, and this notice is
not legal advice.

## Fonts

- **Inter** (Debian `fonts-inter`, SIL Open Font License 1.1). The caption
  font: the render's `"Tamtree Caption"` family resolves to it through a
  fontconfig alias. The OFL allows redistribution and embedding.
- **DejaVu** (Debian `fonts-dejavu-core`, a free licence derived from Bitstream
  Vera). The fallback for glyphs Inter lacks.

v1 ships no CJK or emoji font. Captions in those scripts render as missing-glyph
boxes. Adding Noto CJK and Noto Color Emoji (both OFL) is a known extension, and
the cost is image size.
