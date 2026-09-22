# syntax=docker/dockerfile:1
# The short-video render image (V3.4): a Tamtree worker image plus everything
# `shortvideo.compose` needs that is not Python — Node, `tamtree-remotion-render`,
# the pre-built Remotion bundle, Chrome Headless Shell, ffmpeg and the caption
# font.
#
# It layers ON TOP of an existing Tamtree image rather than replacing it, and it
# does **not** install the shortvideo plugin: plugin install is a build-time step
# of the product image (SEC-G1, `deploy/docker/third-party-plugins.txt`), and
# this file must not become a second place loadable code comes from. Build the
# product image with `shortvideo` in its plugin set, then:
#
#   docker build -f renderer/Dockerfile \
#     --build-arg TAMTREE_IMAGE=tamtree:<tag> -t tamtree-video:<tag> renderer/
#
# Nothing here is fetched at render time. The render child is network-off
# (SEC-D1), and every path the renderer needs is absolute and baked in:
#   /opt/tamtree/remotion/bundle                    (TAMTREE_REMOTION_BUNDLE)
#   /opt/tamtree/chrome/chrome-headless-shell        (TAMTREE_REMOTION_BROWSER)
#   /usr/local/bin/tamtree-remotion-render          (TAMTREE_REMOTION_BIN)
#
# A Remotion licence key is per-deployment configuration
# (TAMTREE_REMOTION_LICENSE_KEY on the worker) and is never baked in — see
# IMAGE-NOTICES.md, which ships in the image at /opt/tamtree/NOTICES.md.

ARG TAMTREE_IMAGE=tamtree:local
# Pinned by digest: the Node binary copied out of this image is what runs every
# render, so moving it is a reviewable diff, not a side effect of a rebuild.
ARG NODE_IMAGE=node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9

# ---- Stage 1: the renderer, its bundle and its browser ---------------------
# NOT pinned to $BUILDPLATFORM: node_modules carries Remotion's native
# compositor, so this stage must run on the target architecture.
FROM ${NODE_IMAGE} AS renderer
WORKDIR /opt/tamtree/remotion
# `npm ci`, never `npm install`: the lockfile is the pin, and a build that
# resolved anything the lockfile does not name must fail.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY tsconfig.json remotion.config.ts ./
COPY src ./src
COPY bin ./bin
COPY scripts ./scripts
COPY test ./test
# Typecheck at build time: a composition that does not compile must not become
# an image. Then the unit suite, which needs nothing but Node.
RUN npm run typecheck && npm test
RUN node scripts/bundle.mjs /opt/tamtree/remotion/bundle
RUN node scripts/ensure-browser.mjs /opt/tamtree/chrome
# SBOM first — `npm sbom` refuses a tree missing anything package.json names,
# even with --omit=dev, so it runs against the full install and omits dev deps
# itself. Then drop the bundler, the CLI and TypeScript (the image renders, it
# never builds) and generate notices from what is actually left.
RUN npm sbom --omit=dev --sbom-format=cyclonedx > sbom.cdx.json \
 && npm prune --omit=dev --no-audit --no-fund \
 && node scripts/notices.mjs node_modules > THIRD_PARTY_NOTICES.md \
 && rm -rf src/*.tsx src/*.ts tsconfig.json remotion.config.ts scripts test

# ---- Stage 2: the worker, with the renderer added ---------------------------
FROM ${TAMTREE_IMAGE} AS runtime
ARG RUNTIME_USER=tamtree
USER root

# - ffmpeg: loudness normalisation (`shortvideo-audio`) and `tamtree.media`.
# - libnss3 … libxcb1: Chrome Headless Shell's shared-library surface (the V0.3
#   spike's set; `--version` below proves it is complete).
# - fonts: Inter is the caption face; DejaVu the fallback. A renderer with no
#   fonts does not fail — it silently draws boxes.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ffmpeg libstdc++6 ca-certificates fontconfig fonts-inter fonts-dejavu-core \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpango-1.0-0 libcairo2 libexpat1 libxcb1 libx11-6 libxext6; \
    rm -rf /var/lib/apt/lists/*

# "Tamtree Caption" is the family the composition asks for; it resolves here,
# so changing the caption face is an image change and never a template edit.
COPY <<'EOF' /etc/fonts/conf.d/60-tamtree-caption.conf
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias binding="strong">
    <family>Tamtree Caption</family>
    <prefer><family>Inter</family></prefer>
    <default><family>DejaVu Sans</family></default>
  </alias>
</fontconfig>
EOF
RUN fc-cache -f

COPY --from=renderer /usr/local/bin/node /usr/local/bin/node
COPY --from=renderer /opt/tamtree /opt/tamtree
COPY IMAGE-NOTICES.md /opt/tamtree/NOTICES.md
RUN set -eux; \
    ln -s /opt/tamtree/remotion/bin/tamtree-remotion-render.mjs /usr/local/bin/tamtree-remotion-render; \
    chmod 0755 /opt/tamtree/remotion/bin/tamtree-remotion-render.mjs; \
    node --version; \
    /opt/tamtree/chrome/chrome-headless-shell --version | tee /opt/tamtree/chrome/VERSION; \
    fc-match 'Tamtree Caption:bold' | tee /opt/tamtree/CAPTION_FONT; \
    fc-match 'Tamtree Caption:bold' | grep -q Inter; \
    ffmpeg -hide_banner -filters | grep -q loudnorm; \
    id "$RUNTIME_USER" >/dev/null 2>&1 || useradd --system --user-group --home-dir /app --shell /usr/sbin/nologin "$RUNTIME_USER"

ENV TAMTREE_REMOTION_BUNDLE=/opt/tamtree/remotion/bundle \
    TAMTREE_REMOTION_BROWSER=/opt/tamtree/chrome/chrome-headless-shell \
    TAMTREE_REMOTION_BIN=/usr/local/bin/tamtree-remotion-render
USER ${RUNTIME_USER}
