"""`shortvideo.minimax_cancel` — stop a clip, and be honest about whether you did.

**Why this is a node and not only a branch inside collection.** V2.4 in the
plan is about what `minimax_collect` does when a Tamtree run is cancelled
mid-poll, and that call site belongs in the collect node, which V2.3 has not
built yet. But the *operation* is needed on its own before that: a run fails
halfway through a shot list and leaves four clips queued at MiniMax, and
without a node the only way to stop them billing is the vendor's console. So
the DELETE lives here, on an error branch an author can actually reach, and
collection will call the same helper when it exists.

**What it will not claim.** MiniMax cancels a `queued` task and refuses a
`running` one — there is no forced stop, and `DELETE` on a finished task is a
*delete* rather than a cancel. That is three outcomes wearing one verb, so this
node reports which one happened instead of returning a bare success. When the
task was already running, the honest sentence is **"local wait stopped;
provider generation may continue and may be billed"** — never "cancelled".

**Why it does not fail the step by default.** Its natural home is a cleanup
path, and a cleanup step that throws because the clip was already running
turns one problem into two. The truth goes in the output item, where the run
log shows it and the flow can branch on it; `fail_if_not_cancelled` is there
for the author who wants the louder version.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import httpx
from tamtree_plugin_sdk import (
    ExecutionContext,
    Item,
    NodeConfigurationError,
    NodeManifest,
    ProgrammaticNode,
)

from tamtree_shortvideo.credentials import MINIMAX_CREDENTIAL_TYPE
from tamtree_shortvideo.minimax import (
    MinimaxUnavailable,
    auth_headers,
    body_of,
    cancel_url,
    request_id,
)

__all__ = ["NODE_NAME", "RUNNING_NOTE", "MinimaxCancelNode"]

NODE_NAME: Final = "shortvideo.minimax_cancel"

#: The sentence V2.4 asks for, word for word, because the difference between
#: it and "cancelled" is the difference between a bill the user expected and
#: one they did not.
RUNNING_NOTE: Final = "local wait stopped; provider generation may continue and may be billed"


class MinimaxCancelNode(ProgrammaticNode):
    """Ask MiniMax to cancel one task, and report what actually happened."""

    name: ClassVar[str] = NODE_NAME
    manifest: ClassVar[NodeManifest] = NodeManifest.model_validate(
        {
            "name": NODE_NAME,
            "display_name": "Short video — MiniMax cancel",
            "description": (
                "Cancel a MiniMax generation by task id. MiniMax can only cancel a task "
                "that is still queued — a running one keeps going and keeps billing — so "
                "this reports which happened rather than claiming it stopped."
            ),
            "category": "Files & media",
            "icon": "icons/shortvideo.svg",
            "kind": "action",
            "inputs": [{"name": "main"}],
            "outputs": [
                {
                    "name": "main",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "cancelled": {"type": "boolean"},
                            "action": {"type": "string"},
                            "status": {"type": "string"},
                            "note": {"type": "string"},
                            "request_id": {"type": "string"},
                        },
                        "required": ["task_id", "cancelled", "note"],
                    },
                }
            ],
            "params": [
                {
                    "name": "task_id",
                    "label": "Task id",
                    "type": "string",
                    "default": "",
                    "required": True,
                    "description": (
                        "The id MiniMax submit returned. Map it from that step — "
                        "{{ $json.task_id }}."
                    ),
                },
                {
                    "name": "fail_if_not_cancelled",
                    "label": "Fail if the task could not be cancelled",
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Off by default, because this step usually runs on a cleanup path "
                        "and a cleanup step that throws turns one problem into two. Either "
                        "way the outcome is in the output — turn this on when a clip that "
                        "kept generating should stop the flow."
                    ),
                },
            ],
            "credentials": [{"type": MINIMAX_CREDENTIAL_TYPE, "required": True}],
        }
    )

    async def execute(self, ctx: ExecutionContext) -> dict[str, list[Item]]:
        headers = await auth_headers(ctx)
        outputs: list[Item] = []
        for item in ctx.input_items() or [Item()]:
            outputs.append(await self._cancel(ctx, item, headers))
        return {"main": outputs}

    async def _cancel(self, ctx: ExecutionContext, item: Item, headers: dict[str, str]) -> Item:
        task_id = str(ctx.param("task_id", item=item) or "").strip()
        if not task_id:
            raise NodeConfigurationError(
                "No task id, so there is nothing to cancel. Map it from the MiniMax submit "
                "step — {{ $json.task_id }}."
            )
        strict = bool(ctx.param("fail_if_not_cancelled", item=item))

        result = await _delete(ctx, task_id, headers=headers)
        if strict and not result["cancelled"]:
            raise NodeConfigurationError(f"MiniMax did not cancel {task_id}: {result['note']}.")
        return Item.model_validate(
            {
                "json": {**item.json_, **result},
                "binary": item.binary or {},
            }
        )


async def _delete(
    ctx: ExecutionContext, task_id: str, *, headers: dict[str, str]
) -> dict[str, Any]:
    """The DELETE, with each of its three outcomes told apart.

    Not routed through `raise_for_response`: a refusal here is usually the
    *expected* answer — the task was already running — and turning it into an
    exception would make the common case the failure case.
    """
    try:
        response = await ctx.http().delete(cancel_url(task_id), headers=headers)
    except httpx.HTTPError as error:
        # Unlike a create, retrying a cancel is free and idempotent: the worst
        # a second DELETE does is find the task already gone.
        raise MinimaxUnavailable(
            f"Could not reach MiniMax to cancel {task_id} ({type(error).__name__}) — "
            "this is worth another attempt."
        ) from error

    payload = body_of(response)
    trace = request_id(payload, response)

    if response.status_code >= 500:
        raise MinimaxUnavailable(
            f"MiniMax answered {response.status_code} to the cancel of {task_id}"
            f"{f' (request id {trace})' if trace else ''} — this is worth another attempt."
        )

    if response.status_code >= 400:
        # The documented refusal: a task already generating cannot be stopped.
        # It is an outcome, not an error, so it is reported rather than raised.
        return {
            "task_id": task_id,
            "cancelled": False,
            "action": "",
            "status": "",
            "note": RUNNING_NOTE,
            "request_id": trace,
        }

    action = str((payload or {}).get("action") or "") if isinstance(payload, dict) else ""
    status = str((payload or {}).get("status") or "") if isinstance(payload, dict) else ""
    # `DELETE` on a finished task deletes it rather than cancelling it. Saying
    # "cancelled" there would claim spend was avoided that was already spent.
    cancelled = action == "cancelled" or status == "cancelled"
    return {
        "task_id": task_id,
        "cancelled": cancelled,
        "action": action,
        "status": status,
        "note": (
            "cancelled before generation started — MiniMax does not charge for this"
            if cancelled
            else "the task had already finished, so this removed it rather than cancelling "
            "it; whatever it generated was billed"
        ),
        "request_id": trace,
    }
