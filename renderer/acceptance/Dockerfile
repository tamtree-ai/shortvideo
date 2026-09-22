# syntax=docker/dockerfile:1
# V3.5 acceptance image: the render image plus the product's Python packages
# and this plugin, so `cases.py` can drive the real `CuratedCliToolRuntime` and
# `run_sandboxed`. A test harness, never shipped — `run.sh` builds it from a
# render image whose base is plain Python, and supplies the two source trees as
# named build contexts (`product`, `plugin`).
ARG VIDEO_IMAGE=tamtree-video:smoke
FROM ${VIDEO_IMAGE}
USER root
RUN --mount=from=product,target=/src/product,rw \
    --mount=from=plugin,target=/src/plugin,rw \
    pip install --no-cache-dir --root-user-action=ignore \
      "/src/product/sdk[testing]" /src/product/plugin-sdk /src/product/nodes /src/plugin
COPY cases.py /opt/acceptance/cases.py
WORKDIR /opt/acceptance
ENTRYPOINT ["python3", "/opt/acceptance/cases.py"]
