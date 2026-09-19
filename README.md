# tamtree-shortvideo

Short-form vertical video generation for [Tamtree](https://github.com/tamtree-ai),
packaged as an installable plugin: narration, footage and composition, as nodes
on the canvas.

> **Status: V0.5 skeleton.** The packaging is real and proven; the pipeline is
> not built yet. What ships today is one self-test node whose only job is to
> prove that this distribution installs, registers and runs on a real Tamtree.
> See [Roadmap](#roadmap).

This is an ordinary Python package. It lives in its own repository, on its own
release schedule, and Tamtree finds it at startup through **entry points** — no
fork, no core patch, no list inside Tamtree to add yourself to.

## Install

```sh
uv pip install tamtree-shortvideo          # once wheels are published
# or, today:
uv pip install 'tamtree-shortvideo @ git+https://github.com/tamtree-ai/shortvideo@main'
```

Restart the Tamtree server and worker. The nodes appear in the palette under
**Files & media**.

Requires a Tamtree whose SDK contracts are `>=1.33, <2` — the `[contracts] sdk`
pin in `tamtree_shortvideo/tamtree-plugin.toml`. An older instance refuses the
plugin at boot with a named error rather than half-loading it.

### Verify the install

Drop a **Short video — Self test** node on a canvas and run it. It needs no
credential and reaches no network. A green run returns:

```json
{ "ok": true, "plugin": "shortvideo", "node": "shortvideo.selftest",
  "contracts_version": "1.33.0", "note": "" }
```

`contracts_version` is what the **host** provides, not what this package was
built against — which is what makes it worth reading after an upgrade.

## Develop

```sh
git clone https://github.com/tamtree-ai/shortvideo && cd shortvideo
uv venv && uv pip install -e '.[testing,dev]'
uv run pytest -q
```

`[tool.uv.sources]` resolves the SDK from a sibling Tamtree checkout at
`../../tamtree`. Without that tree, point the two entries at the git URL
instead.

The conformance gate is Tamtree's own, run against a checkout:

```sh
tamtree plugin test .
```

It validates `tamtree-plugin.toml` against the §17.8 schema and then runs the
contract suite. For live iteration against a running instance, point
`TAMTREE_DEV_PLUGINS_DIR` at the directory holding this repo — dev only; boot
stops if it is set under `TAMTREE_ENV=prod`.

## Design notes

**Node ids carry this plugin's own prefix** (`shortvideo.*`), never `tamtree.*`
— that namespace is reserved for in-tree nodes, and an install must never be
able to shadow a core node. A test enforces it.

**Names are provider-specific where the contract is.** A MiniMax-only operation
is called `shortvideo.minimax_*`, not `shortvideo.video_generate`: a generic
name would silently freeze one vendor's parameters. Generic nodes come only
after two implementations prove a shared contract.

**Permissions are declared when a node needs them, not before.** The manifest
claims no `network`, no `secrets` and an empty egress allowlist today, because
the self-test node talks to nothing. Waves 1–2 add each one alongside the node
that uses it, so every permission in this manifest has a reviewer.

## Roadmap

| Wave | Ships | Status |
|---|---|---|
| **0** | Plugin skeleton — manifest, entry point, contracts pin, contract tests | **done** |
| **1** | `shortvideo.google_tts` — narration audio + caption timepoints | not started |
| **2** | `shortvideo.minimax_submit` / `shortvideo.minimax_collect` — footage | not started |
| **3** | `shortvideo.compose` — Remotion composition behind a curated backend | not started |
| **4** | Published template, attachment-aware approval, recovery | not started |

`shortvideo.selftest` is scaffolding and **Wave 1 removes it**. That removal is
safe only because nothing has been published yet — once a version is released,
a node id is a contract.

Composition (Wave 3) is **self-hosted only**: curated tools are refused on
hosted profiles by design, and a headless browser rendering attacker-influenced
content is not a boundary a shared sandbox can hold.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
