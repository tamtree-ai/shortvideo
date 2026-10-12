# Guide video (template)

This template turns a guide brief into a narrated, screen-recorded 16:9 user guide of your own Tamtree. A reviewer approves a half-size draft, and then the final version renders. **Self-hosted only.**

```
brief → guide (model, or passed in) → narration → scene file paced by the narration
      → masked capture, one clip per section → guide timeline → draft → approval → final → save
```

## Before the first run

**On the worker,** install `tamtree-shortvideo` and `tamtree-browser-capture`, then set:

| Setting | Required | What it's for |
|---|---|---|
| `TAMTREE_PLUGIN_BROWSER_CAPTURE_CLI` | Yes | The capture CLI |
| `TAMTREE_PLUGIN_BROWSER_CAPTURE_ALLOW` | Only if the app isn't on loopback | The capture allowlist |
| `TAMTREE_REMOTION_TIMEOUT_S` | For guides over about 3 minutes | Raises the render time limit |

**Bind three credentials:**

- **`openrouter`:** an OpenRouter key with credits, for the narration.
- **`session`** (`browser_session`): a Playwright storage state for the account to record as.
- **`mask`** (`browser_capture_mask`): the literals, patterns and selectors to blur. Emails, key-shaped strings and password fields are blurred even without it.

## Writing a guide

Pass a written guide on the trigger's `guide` to record it exactly as written. If you leave `guide` out, the model writes one from `brief`, but it can't see the app. A guide is a list of sections, and each section has:

- **`id`:** lowercase letters, digits and hyphens. It's also the capture scene id.
- **`title`:** shown on a card as the section opens, and used as the chapter name.
- **`lines`:** the narration, one spoken sentence per line.
- **`steps`:** what to do in the app. These use browser-capture's scene steps: `goto`, `click`, `hover`, `fill`, `press`, `wait_for`, `scroll` and `pause`, each with an optional `hold_ms`.
- **`callouts`** (optional): `{"step": <index into steps>, "text": "..."}`, which draws a labelled highlight on that step's target.

Each section's recording is told to last at least as long as its lines take to speak, so put the actions near the start of a section and let the narration explain them.

```json
{
  "title": "Inspect an HTTP Request step",
  "sections": [
    {
      "id": "open-step",
      "title": "Open the step",
      "lines": [
        "This is the Daily Tech News Digest workflow.",
        "Click the TechCrunch fetch step to see how it is configured."
      ],
      "steps": [
        {"action": "goto", "path": "/flows/<flow id>/edit", "hold_ms": 1500},
        {"action": "click", "target": {"node": "fetch_techcrunch"}, "hold_ms": 1200}
      ],
      "callouts": [{"step": 1, "text": "An HTTP Request step"}]
    },
    {
      "id": "inspect-output",
      "title": "Inspect the output",
      "lines": ["After a run, the output tab shows exactly what the request returned."],
      "steps": [
        {"action": "click", "target": {"text": "Output", "exact": true}, "hold_ms": 1500},
        {"action": "scroll", "target": {"text": "status"}, "by": 400}
      ]
    }
  ]
}
```

## What a run returns

- **The guide's mp4,** saved as `guide-<digest>`.
- **The SRT and the chapter list,** in the saved asset's metadata and on the final `done` step. Paste them into the upload form.

Uploading is by hand.
