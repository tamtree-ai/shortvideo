"""V3.4: Remotion usage is reported from the node, and never fails a render.

The claims worth pinning are the ones V0.4 made binding: no key means no
report *and says so*; a key is reported through `ctx.http()` with the exact body
`registerUsageEvent()` sends; a failed report is a status, not an exception;
and the key never appears in anything the run keeps.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tamtree_plugin_sdk.testing import FakeExecutionContext

from tamtree_shortvideo.licensing import LICENSE_KEY_ENV, USAGE_URL, report_render

KEY = "rm_live_0123456789abcdef"


def _ctx(handler: object) -> tuple[FakeExecutionContext, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator, no-any-return]

    ctx = FakeExecutionContext(inputs={"main": []}, params={})
    ctx.http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(record)
    )
    return ctx, seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "billable": True, "classification": "billable"}
    )


async def test_no_key_reports_nothing_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LICENSE_KEY_ENV, raising=False)
    ctx, seen = _ctx(_ok)
    report = await report_render(ctx)
    assert report["status"] == "not_configured"
    assert LICENSE_KEY_ENV in report["detail"]
    assert seen == []


async def test_a_key_is_reported_with_the_body_remotion_itself_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LICENSE_KEY_ENV, KEY)
    ctx, seen = _ctx(_ok)
    report = await report_render(ctx)
    assert report["status"] == "reported"
    (request,) = seen
    assert str(request.url) == USAGE_URL
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "event": "cloud-render",
        "apiKey": KEY,
        "host": None,
        "succeeded": True,
        "isStill": False,
        "isProduction": True,
    }


async def test_the_free_licence_reports_with_a_null_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LICENSE_KEY_ENV, "free-license")
    ctx, seen = _ctx(_ok)
    await report_render(ctx)
    assert json.loads(seen[0].content)["apiKey"] is None


async def test_an_unreachable_host_is_a_status_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LICENSE_KEY_ENV, KEY)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused while sending {KEY}")

    ctx, _ = _ctx(refuse)
    report = await report_render(ctx)
    assert report["status"] == "failed"
    assert KEY not in report["detail"]


async def test_a_refusal_carries_remotion_s_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LICENSE_KEY_ENV, KEY)
    ctx, _ = _ctx(lambda request: httpx.Response(401, json={"success": False, "error": "bad key"}))
    report = await report_render(ctx)
    assert report["status"] == "failed"
    assert "HTTP 401" in report["detail"]
    assert "bad key" in report["detail"]


async def test_a_non_json_answer_is_a_failure_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LICENSE_KEY_ENV, KEY)
    ctx, _ = _ctx(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    assert (await report_render(ctx))["status"] == "failed"
