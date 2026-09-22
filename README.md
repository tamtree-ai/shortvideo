# tamtree-shortvideo

Short-form vertical video generation for [Tamtree](https://github.com/tamtree-ai),
packaged as an installable plugin: narration, footage and composition, as nodes
on the canvas.

> **Status: Wave 3 in progress.** Narration and footage work end to end —
> submit, collect, cancel — and a published flow loops one beat at a time.
> **Composition landed as `shortvideo.compose`** (V3.2/V3.3): the timeline
> contract, the curated `remotion` backend and the render executable are all
> here. What is not here yet is the **image** that carries the renderer —
> until V3.4 builds it, a worker reports the tool as unavailable rather than
> rendering. Every claim here is unit-proven against recorded transport;
> nothing has yet run against a live provider key. See [Roadmap](#roadmap).

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

Requires a Tamtree whose SDK contracts are `>=1.36, <2` — the `[contracts] sdk`
pin in `tamtree_shortvideo/tamtree-plugin.toml`. An older instance refuses the
plugin at boot with a named error rather than half-loading it. The floor is
1.36 because the two things `shortvideo.compose` is built on do not exist below
it: the curated-backend contract in the SDK (1.35.0) and
`MediaLimits.limit_address_space` (1.36.0), without which a browser-based
renderer cannot start at all.

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
billed per character, and a paid call reported without a cost never reaches the
workspace budget at all — not even as an unpriced call its block count could
catch, so this step refuses to run rather than spend money nothing will record.
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

### Short video — OpenRouter Text-to-Speech (`shortvideo.openrouter_tts`)

The alternative to `google_tts` for anyone who does not want to stand up a
Google Cloud project: narration through **Gemini 3.1 Flash TTS**, billed
through an OpenRouter API key instead. Same output shape
(`duration_seconds`, `marks`, `audio`, `captions`) so the compositor cannot
tell which node produced an item — but it gets there differently, because
OpenRouter's `/audio/speech` endpoint has no SSML and no `<mark>` timepoints
to lean on.

Two input modes, not three — there is no SSML mode, because this provider does
not accept SSML:

| Input | For |
|---|---|
| **Plain text** | One block of narration, one call. Fast, but no per-phrase caption timing. |
| **Phrase list** | One call *per phrase*, stitched together. Slower and costs one call each, but every caption timing is exact — measured from that phrase's own synthesized audio, the same way `google_tts` measures its whole clip. |

Because each phrase is its own independent call rather than one continuous
reading, it is also its own independent utterance: the prosody a single-pass
narrator gives a mid-paragraph sentence is not what stitched-together
one-sentence syntheses sound like. `phrase_gap_seconds` (default `0.15`)
inserts a short silence between phrases to soften the seam — it does not
reproduce one continuous take.

**Set up the credential first.** Under Credentials, add an **OpenRouter API**
key from [openrouter.ai](https://openrouter.ai) (Settings → Keys). Unlike the
Google credential, this one can be tested for real — *Test connection* probes
a model listing, which generates nothing.

**Cost is read from OpenRouter's own ledger, not entered by hand.** After each
`/audio/speech` call, the node looks up that call's `total_cost` on
`GET /api/v1/generation`, with a short bounded retry for the ordinary race
where the ledger has not indexed the call yet. If the ledger genuinely never
catches up, the call is reported **unpriced** (`priced: false`, `cost_usd:
""`) rather than guessed at zero — the audio is not thrown away over a slow
ledger, but the workspace budget is told honestly that this call's price is
unknown.

### Short video — MiniMax submit (`shortvideo.minimax_submit`)

Starts one video generation and returns its `task_id`. It does **not** wait for
the clip — `shortvideo.minimax_collect` does that, and the split is the point:
`ExecutionContext` has no mid-node checkpoint, so a node that submitted and then
polled could not write the task id down until the whole step succeeded, and a
worker restart in the middle would submit a second time and bill a second time.

Set up a **MiniMax API** credential first (Account Management → API Keys). Unlike
the Google one, this credential can be tested for real: *Test connection* probes
a model listing, which generates nothing.

The credential also asks for **your rate in USD per generated second**, and will
not save without one. MiniMax publishes no rate for the H3 models — pay-as-you-go
or contact sales — so nothing is filled in for you; the number is the one on your
plan. `0` is a permitted answer and means *I accept that these generations stay
unpriced* (see the cost note under collect). It lives on the credential rather
than on a step because it is a fact about the account, and because a template
install already forces the credential slot to be filled.

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

**Images travel as data URIs.** Point `first_frame`, `last_frame` or
`reference_images` at a binary property on the incoming item and the bytes go
inline; MiniMax caps one image at 30 MB and the whole request at 64 MB, so a
frame pair fits comfortably even after base64's extra third. An `https://` URL
or an `mm_file://` reference is passed through instead, which is the escape
hatch when an image is too big to inline.

Every image is checked before the request, and its format is read from the
**bytes** rather than from the attachment's declared type — so a `.png` that is
really something else is caught here rather than by MiniMax. Formats (JPEG, PNG,
WEBP, HEIC, HEIF), 256–5760 px per side, aspect ratio 0.4–2.5, and the
image-to-video / reference-to-video exclusion are all refused locally. HEIC and
HEIF skip the dimension check: reading those means walking ISO-BMFF boxes, and
MiniMax judges them instead.

### Short video — MiniMax collect (`shortvideo.minimax_collect`)

Takes the `task_id` from the submit step, waits for the generation to finish,
and saves the clip as an attachment on the item.

**It keeps the retry budget submit refuses.** A poll creates nothing and costs
nothing, so an unreachable provider is safe to retry here; that asymmetry is the
whole reason the two nodes are separate. Set `retry_on_fail: true` on this step
and `false` on submit — the node's own judgement is not enough, because the
engine would otherwise re-run the activity underneath it.

| Param | Default | |
|---|---|---|
| **Give up waiting after** | 900s | The budget for the whole wait, not one poll. |
| **First poll interval** | 5s | Grows ×1.5 up to 30s — a tight poll buys nothing on a job that takes minutes, and an unbounded gap outlives the result URL. |
| **Refuse a clip larger than** | 256 MB | Enforced *while streaming*, so an over-size body is abandoned mid-transfer rather than buffered and then refused. |
| **Override the rate, USD per second** | *(unset)* | Empty means the rate on the MiniMax credential. Fill it in only to price one step differently. See below. |

**Running out of time is not a failure.** It raises `MinimaxNotReady`, which
says the three things you need: nothing was cancelled, the clip is still
generating and will still be billed, and re-running this step on the same id
collects it. MiniMax keeps a task queryable for 7 days.

**The container is read from the clip's magic bytes, never its `Content-Type`.**
The failure this guards against is a CDN serving an `AccessDenied` XML page as
`video/mp4`; saving that under a video MIME type would only move the break into
whatever opens it next.

**The API key goes to MiniMax and never to the CDN.** The result URL carries its
own signature, so there is nothing to gain by handing a workspace secret to
whatever host it resolves to. An expired signature (a 4xx from the CDN) stays
retryable, because a re-run re-queries for a fresh URL.

> **Cost is asked for, never assumed.** MiniMax publishes no per-second USD rate
> for the H3 models, so there is no honest number to hard-code — a node that
> invented one would put a fabricated figure into a budget that stops people's
> work. The rate is therefore a **required field on the MiniMax credential**,
> answered once per account, and this node prices the provider's own
> `usage.total_seconds` against it.
>
> **`0` is allowed, and it is a real choice with a real consequence.** An
> unpriced generation is invisible twice over: no `usage_ledger` row is written
> at all, so the spend misses the workspace budget *and* is never counted in
> `unpriced_calls` — which means a workspace's `unpriced_block_count` guard can
> never fire on it, whatever it is set to. The provider's seconds still travel
> in the output (`billed_seconds`, `priced: false`), and that is the only trace
> left. Pick `0` only if you mean it.

### Short video — MiniMax cancel (`shortvideo.minimax_cancel`)

Stops a generation by task id — for an error branch, or to stop clips billing
after a run fell over halfway through a shot list.

One `DELETE` does three different things depending on the task's state, so this
node reports **which** rather than returning a bare success:

| Task state | What happens | `cancelled` |
|---|---|---|
| `queued` | Genuinely cancelled, and MiniMax does not charge | `true` |
| `running` | **Refused.** There is no forced stop | `false` |
| `succeeded` / `failed` | Deleted, not cancelled — whatever it generated was billed | `false` |

When the clip was already running, the note reads *"local wait stopped; provider
generation may continue and may be billed"*. It will not say "cancelled", because
that would be the difference between a bill you expected and one you didn't.

It does **not** fail the step by default — a cleanup step that throws because the
clip was already running turns one problem into two. Turn on *Fail if the task
could not be cancelled* when you want the louder version.

### Short video — compose (`shortvideo.compose`)

Renders one **TimelineV1** document — narration, per-beat footage, captions and
transitions — to a 1080×1920 mp4. **Self-hosted only:** it runs a local
renderer in the shared subprocess sandbox, which hosted multi-tenant does not
support (SEC-D3), and the step is refused there before it reads an input.

It needs the **video render image**, which carries `tamtree-remotion-render`,
the pre-built compositions, Chrome Headless Shell and the caption font. Without
it the step reports the tool as unavailable on this worker rather than failing
a render.

| Param | Default | |
|---|---|---|
| **Timeline** | *(blank)* | The document to render. Blank reads `{{ $json.timeline }}` from the incoming item, which is what the template wires. |
| **Draft quality** | off | Renders at half size (540×960) for the approval preview. The *same* composition and the same frame boundaries — scale is the only difference, which is what makes approving the draft and rendering the final one decision. |
| **Output attachment name** | `video` | What the finished mp4 is returned under. |

Every clip and the narration must reach this step as an **attachment**; the
timeline names them by `BinaryRef` id, and an id no incoming item carries is
named in the error rather than discovered inside a browser.

| It refuses | Because |
|---|---|
| A timeline that disagrees with itself — a gap between beats, a total that does not match, a caption outside its beat | The frame math is re-derived, never trusted. A document taken on faith renders as a plausible *wrong* video, which is the one failure worse than an error. |
| A crossfade longer than the outgoing clip's kept tail | A transition is paid for out of the tail the trim left. One the clip cannot pay for is caught at validation, not discovered at render. |
| Captions on `short-plain`, or a beat without one on `short-captioned` | Otherwise the two templates would be indistinguishable in the timeline and different only in output. |
| A clip or narration in a format the pipeline does not produce | Named, with the type, rather than handed to a decoder. |

Everything it refuses, it refuses **before fetching a byte** — D9's promise is
about cost, and it is only true if nothing expensive happens first.

**Concurrency is capped per worker process, not per deployment.** At most one
render per workspace runs at a time on a given worker (raise it with
`TAMTREE_SHORTVIDEO_MAX_RENDERS`); a burst waits for a slot rather than
failing. This is *not* tenant fairness — a second worker has its own gate and
knows nothing about this one. Size a worker with the measured figure: **~880 MB
of RSS and ~1.7 cores per concurrent render.**

The renderer itself lives in [`renderer/`](renderer/) and has its own README,
including why there is a loopback web server inside it and what it deliberately
does not do (integrated loudness normalization, sidechain ducking).

## Flows

Two importable flows ship in `flows/`. They are Wave 2's harness, not the
finished product — Wave 4's `Short-form video` template will use the same body.

| File | |
|---|---|
| `generate-one-beat.yaml` | The loop body: submit → collect → save asset → relabel. Starts with a Sub-workflow Trigger, so a Loop node in the parent runs it once per beat. |
| `beats-to-clips.yaml` | The smallest parent that makes the body runnable: a beat list in, a clip per beat out. |

**Publish the body before the parent can run.** A Loop resolves its body's
published version at plan time, and installed flows arrive as drafts.

Three things in there are load-bearing rather than scaffolding:

- **`on_item_error: skip` on the Loop, plus the `status` port.** "A failed beat
  must not discard successful siblings" is a property of the *parent*, not the
  body. The `status` port is the only place the pass/outcome correlation
  survives — `main` drops a skipped pass entirely, and Merge appends rather than
  zips.
- **`retry_on_fail: false` on submit, `true` on collect.** The step setting has
  to agree with the node, or the engine re-runs the activity underneath it. This
  is the one place an author would actually break the submit/collect split.
- **`timeout_s` (660) outlives `max_wait_seconds` (600),** so the step is not
  killed from outside while the node still believes it has time left — otherwise
  you never see the node's own message.

`beat_number` travels on the item and is required by the body's input schema:
under `on_item_error: skip` a dropped beat shifts every later index, so nothing
downstream may identify a beat by position.

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
| **1b** | `shortvideo.openrouter_tts` — narration via Gemini 3.1 Flash TTS, no GCP account needed | **done** |
| **2** | `shortvideo.minimax_submit` / `shortvideo.minimax_collect` — footage | **done** — submit, collect, cancel, image inputs, and the published Loop body |
| **3** | `shortvideo.compose` — Remotion composition behind a curated backend | **node, backend and renderer done** — the image that carries them (V3.4) and the negative security suite (V3.5) are next |
| **4** | Published template, attachment-aware approval, recovery | not started |

`shortvideo.selftest` was Wave 0 scaffolding and Wave 1 removed it. That was
safe only because nothing had been published yet — once a version is released,
a node id is a contract.

Composition (Wave 3) is **self-hosted only**: curated tools are refused on
hosted profiles by design, and a headless browser rendering attacker-influenced
content is not a boundary a shared sandbox can hold.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
