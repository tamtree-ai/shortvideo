# Short-form video

Type a topic; get a narrated, captioned 1080×1920 vertical short — after you
have watched the draft and approved it.

```
topic → script → shot list → narration → one clip per beat
      → assemble → half-size draft → your approval → final render → saved
```

## Before you install

**Self-hosted only.** The render runs Chrome Headless Shell and ffmpeg inside
the worker's local sandbox. Hosted multi-tenant Tamtree refuses the render step
before it reads any input.

You need:

| | |
|---|---|
| The `shortvideo` plugin | Installed on the worker, with the render image's Chrome and ffmpeg. See the plugin README. |
| A Google Cloud service account | With Text-to-Speech enabled. Bound to the `google` slot. |
| A MiniMax API key | With your per-second rate filled in on the credential. Bound to the `minimax` slot. |
| A default chat model | For the script. Any workspace default works. |

Two things to do after installing:

1. **Publish `Generate one beat` first.** The main flow's Loop runs the published
   version of it, and installed flows arrive as drafts.
2. **Set the narration price.** Open `Narrate` in `Short-form video` and fill in
   *Price per 1M characters* from Google's pricing page for your voice tier. The
   step refuses to run at 0: an unpriced call would never reach your budget.

## What one run can cost

| Step | Calls | Notes |
|---|---|---|
| Script | 1 chat call | Your workspace's default model. Skipped on a replay. |
| Narration | 1 Google synthesis | Skipped when the same lines and voice were narrated before. |
| Clips | at most 8 MiniMax generations | 6 s, 768P each. The shot list refuses a longer script before anything is paid for. |
| Draft + final | 2 local renders | CPU only, no provider. |

**How long:** each generation is allowed up to 10 minutes, so the worst case
before the draft is about 80 minutes. A render measured about one second per
second of video on the reference worker, and each is capped at 15 minutes of
CPU.

## Approving

The run stops at **Approve this short?** with the draft playing in the banner
and `Approving · timeline <digest>`. Approving renders *that* timeline at full
size — the final step refuses any other — and saves it to the Asset Library as
`short-<digest>`. Your decision, your note and the digest are recorded in the
audit log.

## When something goes wrong: re-run with the script

Every clip and narration is saved under a name derived from exactly what
produced it, and each paid step looks for it first. So recovery is always the
same move: **run the flow again with `script` set to the previous run's shot
list.** It is on the `rejected` step's output (with the reviewer's note), or on
`Shot list`'s output.

| Situation | What you change | What is paid for again |
|---|---|---|
| A beat failed (the assemble step names it) | Nothing | Only that beat |
| The draft was rejected for one shot | That beat's `visual_prompt` | Only that beat |
| Wrong line in the narration | That beat's `narration_text` | The narration, and nothing else |
| Wrong look (captions, crossfade) | `Assemble timeline`'s params | Nothing — only the renders run |

A rejection never regenerates anything on its own.
