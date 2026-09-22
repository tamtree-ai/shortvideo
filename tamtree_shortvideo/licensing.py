"""Remotion usage reporting — from the node, never from the render (V3.4).

Remotion's Automator terms want every successful programmatic render reported
against the operator's licence key (V0.4, `01-plan.md` §5.4). Remotion's own
way of doing that is to pass `licenseKey` to `renderMedia()`, which POSTs a
usage event from inside the render process once the mp4 is stitched.

**That route is closed here, and it fails in the worst possible way.** The
render child is network-off (SEC-D1). `@remotion/licensing` 4.0.526 treats the
refused connection as retryable, backs off 1+2+4 seconds, logs "Failed to send
usage event" to a stderr nobody reads, and then *resolves the render as a
success*. Seven seconds added to every render, and a compliance report that
never left the box with nothing anywhere to say so. So the renderer is never
given a key, and the report is made here instead, through `ctx.http()` — the
reviewed egress path, SSRF policy and all — after the render has succeeded.

**The wire call is the one `registerUsageEvent()` makes**, byte for byte:
`POST https://www.remotion.pro/api/track/register-usage-point` with
`{event, apiKey, host, succeeded, isStill, isProduction}`. `registerUsageEvent`
is a public export of `@remotion/licensing`; the endpoint behind it is not a
documented API, and whether a report made from outside the render discharges
an Automator's duty is a question for Remotion (§5.4 says so). Until they
confirm it, this is the best-available report, not a certified one.

**What it never does is fail a render.** V0.4's binding consequence: a render
with no key configured still renders and says plainly that it reported nothing;
a report that fails says that too. The status rides the node's output, so an
operator can see — per render, in the run — whether their licence was told.
"""

from __future__ import annotations

import os
from typing import Any, Final, Literal, TypedDict

import httpx
from tamtree_plugin_sdk import ExecutionContext

__all__ = [
    "LICENSE_KEY_ENV",
    "USAGE_URL",
    "UsageReport",
    "report_render",
]

#: Per-deployment configuration, never a shipped constant (V0.4): a self-hoster
#: supplies their own key, and Tamtree's must not travel in the image.
LICENSE_KEY_ENV: Final = "TAMTREE_REMOTION_LICENSE_KEY"

#: `@remotion/licensing`'s `HOST` + the path `internalRegisterUsageEvent` POSTs
#: to. Pinned to the 4.0.526 source; re-check it whenever the renderer's
#: Remotion version moves.
USAGE_URL: Final = "https://www.remotion.pro/api/track/register-usage-point"

#: The value Remotion itself accepts for "I qualify for the Free License". It
#: still reports, with a null key — mirrored rather than second-guessed.
_FREE_LICENSE: Final = "free-license"

_TIMEOUT_S: Final = 10.0


class UsageReport(TypedDict):
    status: Literal["not_configured", "reported", "failed"]
    detail: str


async def report_render(ctx: ExecutionContext) -> UsageReport:
    """Report one successful render. Never raises: every outcome is a status."""
    key = (os.environ.get(LICENSE_KEY_ENV) or "").strip()
    if not key:
        return {
            "status": "not_configured",
            "detail": (
                f"No Remotion usage was reported: {LICENSE_KEY_ENV} is not set on this worker. "
                "Operators above Remotion's free-licence threshold must configure a licence key."
            ),
        }

    body: dict[str, Any] = {
        "event": "cloud-render",
        "apiKey": None if key == _FREE_LICENSE else key,
        "host": None,
        "succeeded": True,
        "isStill": False,
        "isProduction": True,
    }
    try:
        response = await ctx.http().post(USAGE_URL, json=body, timeout=_TIMEOUT_S)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        # The key is never echoed: an error string can end up in a run's
        # output, and the key is the one thing here worth stealing.
        return {
            "status": "failed",
            "detail": f"The Remotion usage report did not go through: {type(error).__name__}.",
        }
    if isinstance(payload, dict) and payload.get("success"):
        return {
            "status": "reported",
            "detail": f"Remotion usage reported (classification: {payload.get('classification')}).",
        }
    reason = payload.get("error") if isinstance(payload, dict) else None
    return {
        "status": "failed",
        "detail": (
            f"Remotion refused the usage report (HTTP {response.status_code})"
            + (f": {str(reason)[:200]}" if reason else ".")
        ),
    }
