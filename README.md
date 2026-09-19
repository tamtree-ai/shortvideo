# tamtree-shortvideo

Short-form vertical video generation for [Tamtree](https://github.com/tamtree-ai),
packaged as an installable plugin: narration, footage and composition, as nodes
on the canvas.

> **Status: Wave 2 in progress.** Narration works end to end and footage can
> be submitted; collection and composition are not built yet. See
> [Roadmap](#roadmap).

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

## Nodes

### Short video — Google Text-to-Speech (`shortvideo.google_tts`)

Turns a paragraph into a narration attachment, its **measured** duration, and
caption timings.

Three ways to give it words:

| Input | For |
|---|---|
| **Plain text** | One block of narration, read as written. |
| **SSML** | Hand-written markup when you want control over pacing, pronunciation or your own `<mark>` placement. |
| **Phrase list** | A list of caption phrases. The node builds the SSML — escaping, mark names, size check — and returns each phrase with the time it is spoken. |

Every `<mark>` comes back as `marks[{name, time_seconds}]`, which is where
captions get their timing without a second model in the path. In phrase-list
mode you get the finished article instead: `captions[{name, text,
start_seconds, end_seconds}]`, each phrase running until the next one starts
and the last to the end of the audio.

```json
["Compound interest is misunderstood.", "Here is the part nobody mentions."]
```

becomes

```json
[{"name": "p0", "text": "Compound interest is misunderstood.",
  "start_seconds": 0.0, "end_seconds": 2.14},
 {"name": "p1", "text": "Here is the part nobody mentions.",
  "start_seconds": 2.14, "end_seconds": 4.02}]
```

The audio arrives as a binary attachment (named `audio` by default) rather than
inline bytes, and whatever the incoming item already carried travels on
untouched.

**Set up the credential first.** Under Credentials, add a **Google service
account**: paste the *whole* JSON key file downloaded from a service account in
a Google Cloud project with the Text-to-Speech API enabled. *Test connection*
can only say "stored" for this type — there is no probe that can mint a Google
token — so the first real proof is a green run of this node. A bad or expired
key fails it by name.

**The price parameter is required, and there is no default.** Text-to-Speech is
billed per character, and a paid call reported without a cost is invisible to
the workspace monthly budget *and* counted against its unpriced-spend block.
Enter the rate for the voice tier you are using from
[Google's pricing page](https://cloud.google.com/text-to-speech/pricing). No
rate is built in on purpose: a vendor price baked into a release goes stale and
quietly under-reports what you are spending.

A few deliberate refusals, each with a named error rather than a quiet
approximation:

| It refuses | Because |
|---|---|
| Input over 5,000 UTF-8 bytes | Google's synchronous limit. Truncating would cut a sentence and the short would never mention it. In phrase-list mode the error names how many phrases *would* have fit, so you know where to split the beat. |
| Audio formats other than WAV, MP3 and Ogg Opus | The timeline is built on an exactly measured duration, and only these three carry one. |
| A voice that returns no caption marks | Support for `<mark>` varies by voice; Studio voices have none. A short with silently missing captions is worse than a failed step. Turn the check off if the captions are genuinely optional. |
| Marks that run backwards or land past the end of the audio | Captions built from them would be wrong after the render, not before it. |

### Short video — MiniMax submit (`shortvideo.minimax_submit`)

Starts one video generation and returns its `task_id`. It does **not** wait for
the clip — `shortvideo.minimax_collect` does that, and the split is the point:
`ExecutionContext` has no mid-node checkpoint, so a node that submitted and then
polled could not write the task id down until the whole step succeeded, and a
worker restart in the middle would submit a second time and bill a second time.

Set up a **MiniMax API** credential first (Account Management → API Keys). Unlike
the Google one, this credential can be tested for real: *Test connection* probes
a model listing, which generates nothing.

Duration and resolution limits differ by model and are checked **locally**, so a
wrong value fails before it reaches a paid API:

| Model | Duration | Resolution |
|---|---|---|
| `MiniMax-H3` | 4–15s | 768P, 2K |
| `MiniMax-H3-Max` | 5–15s | 480P, 768P |

**It will not retry a create call on its own.** MiniMax documents no idempotency
key, so a retried create is a second charge that nothing can recognise as a
duplicate. A `429` is safe to retry and is retried; a `5xx`, a timeout or a
dropped connection is *not*, because none of them says whether the clip was
accepted. Those fail with a named error carrying MiniMax's `request_id`, and you
decide whether to resubmit. This is a deliberate limit, not an oversight: **this
plugin never claims exactly-once provider spend.**

Image and reference inputs (`first_frame`, `last_frame`, `reference_image`) are
not here yet — a workspace attachment is not a public URL, and that transport is
its own piece of work.

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

**Permissions are declared when a node needs them, not before.** `network` and
`secrets` arrived with the token mint that first needed them, and the egress
allowlist names only the two hosts this plugin actually talks to. Waves 2–3 add
each one alongside the node that uses it, so every permission in this manifest
has a reviewer.

**Escaping, mark naming and the size check live in the node, not in an
expression field.** A script step that writes "Marks & Spencer's Q3 < Q4" into
a hand-built `<speak>` template produces malformed XML and an
`INVALID_ARGUMENT` that names nothing useful; two phrases given the same mark
name produce a caption track that silently mismatches its text. Those are not
edge cases, they are what a real script does on a Tuesday — so phrase-list
mode handles all three once.

**Money is never spent on a guess.** Both paid nodes refuse locally what they
can check locally — input size, the model matrix, the aspect ratio — so a wrong
value costs a validation error rather than a run and a bill. And where a
provider's answer is *ambiguous* rather than failed, the node stops and says so
instead of retrying into a second charge.

**Durations are measured, never estimated.** Narration length is what decides
where a beat splits, how a clip is trimmed and when a caption appears. So the
audio format dropdown offers only containers whose playing time can be read
exactly from the bytes, and a format that cannot be measured is refused rather
than guessed at.

## Roadmap

| Wave | Ships | Status |
|---|---|---|
| **0** | Plugin skeleton — manifest, entry point, contracts pin, contract tests | **done** |
| **1** | `shortvideo.google_tts` — narration audio + caption timepoints | **done** |
| **2** | `shortvideo.minimax_submit` / `shortvideo.minimax_collect` — footage | **in progress** — submit done |
| **3** | `shortvideo.compose` — Remotion composition behind a curated backend | not started |
| **4** | Published template, attachment-aware approval, recovery | not started |

`shortvideo.selftest` was Wave 0 scaffolding and Wave 1 removed it. That was
safe only because nothing had been published yet — once a version is released,
a node id is a contract.

Composition (Wave 3) is **self-hosted only**: curated tools are refused on
hosted profiles by design, and a headless browser rendering attacker-influenced
content is not a boundary a shared sandbox can hold.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
