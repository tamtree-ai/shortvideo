#!/usr/bin/env bash
# Build the V3.5 acceptance image and run the negative/resource cases.
#
#   renderer/acceptance/run.sh [PRODUCT_CHECKOUT] [VIDEO_IMAGE]
#
# PRODUCT_CHECKOUT defaults to ~/sites/tamtree and must be on a branch that
# carries contracts >= 1.36 (MediaLimits.limit_address_space, `lo` up in the
# netns). VIDEO_IMAGE defaults to tamtree-video:smoke (renderer/Dockerfile
# built with TAMTREE_IMAGE=python:3.12-slim-bookworm).
#
# The container runs as root with CAP_SYS_ADMIN **and CAP_NET_ADMIN**, and
# **with** its network, so the netns SEC-D1 creates is the only thing between
# the render and the internet. NET_ADMIN is not optional: without it
# `unshare(CLONE_NEWNET)` succeeds but bringing `lo` up in the new namespace
# fails (EPERM, swallowed), and every render dies with ENETUNREACH on its own
# loopback media server (V3.5 finding). `--memory` is the deployment-level ceiling a render is sized
# against (~880 MB each); the 8K case runs under it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
plugin="$(cd "$here/../.." && pwd)"
product="${1:-$HOME/sites/tamtree}"
video="${2:-tamtree-video:smoke}"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
# Only what pip needs: no venvs, no node_modules, no caches.
for pkg in sdk plugin-sdk nodes; do
  rsync -a --exclude '__pycache__' --exclude '.venv' "$product/packages/$pkg" "$stage/product/"
done
rsync -a --exclude '.venv' --exclude 'node_modules' --exclude '__pycache__' \
  --exclude 'renderer' --exclude '.git' "$plugin/" "$stage/plugin/"

docker build -q \
  --build-context product="$stage/product" \
  --build-context plugin="$stage/plugin" \
  --build-arg VIDEO_IMAGE="$video" \
  -t tamtree-video-acceptance "$here" >/dev/null

docker run --rm --user root --cap-add SYS_ADMIN --cap-add NET_ADMIN --memory 2g tamtree-video-acceptance
