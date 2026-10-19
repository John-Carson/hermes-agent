"""Tests for the Seed platform-plugin adapter (Seed's web and iOS app as the chat client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_seed = load_plugin_adapter("seed")
SeedAdapter = _seed.SeedAdapter

EXTRA = {"url": "https://seed.example", "relay_token": "relay-tok", "inbound_secret": "s3cret"}


def _adapter() -> SeedAdapter:
    return SeedAdapter(PlatformConfig(enabled=True, extra=dict(EXTRA)))


def _app(adapter: SeedAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/messages", adapter._handle_inbound_message)
    app.router.add_post("/actions", adapter._handle_inbound_action)
    return app


class _Recorder:
    """httpx transport that records requests and answers like Seed's API."""

    def __init__(self, status: int = 201):
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": 42})


def _patched_client(recorder: _Recorder):
    real = httpx.AsyncClient
    return patch.object(_seed.httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(recorder), **kwargs))


def test_requirements_need_url_token_and_secret(monkeypatch):
    for name in ("SEED_URL", "SEED_RELAY_TOKEN", "SEED_INBOUND_SECRET"):
        monkeypatch.setenv(name, "x")
    assert _seed.check_requirements() is True
    monkeypatch.delenv("SEED_INBOUND_SECRET")
    assert _seed.check_requirements() is False
    assert _seed.validate_config(PlatformConfig(enabled=True, extra=dict(EXTRA))) is True


@pytest.mark.asyncio
async def test_inbound_message_needs_the_shared_secret_and_reaches_the_agent():
    adapter = _adapter()
    adapter.handle_message = AsyncMock()
    async with TestClient(TestServer(_app(adapter))) as cli:
        denied = await cli.post("/messages", json={"chat_id": "seed:1", "user_id": "1", "text": "hi"},
                                headers={"X-Seed-Secret": "wrong"})
        assert denied.status == 403
        adapter.handle_message.assert_not_awaited()

        accepted = await cli.post("/messages", json={"chat_id": "seed:1", "user_id": "1", "user_name": "Jack",
                                                     "text": "plan my week", "message_id": "m-7"},
                                  headers={"X-Seed-Secret": "s3cret"})
        assert accepted.status == 202
        event = adapter.handle_message.await_args.args[0]
        assert (event.text, event.source.chat_id, event.source.user_id, event.message_id) == (
            "plan my week", "seed:1", "1", "m-7")


@pytest.mark.asyncio
async def test_inbound_message_without_text_is_rejected():
    adapter = _adapter()
    adapter.handle_message = AsyncMock()
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/messages", json={"chat_id": "seed:1", "user_id": "1"},
                              headers={"X-Seed-Secret": "s3cret"})
    assert resp.status == 400
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_and_edit_post_to_seed_with_the_relay_token():
    adapter, recorder = _adapter(), _Recorder()
    with _patched_client(recorder):
        sent = await adapter.send("seed:1", "Here is your week.", reply_to="m-7")
        edited = await adapter.edit_message("seed:1", "42", "Here is your week, updated.", finalize=True)

    assert (sent.success, sent.message_id, edited.success) == (True, "42", True)
    create, edit = recorder.requests
    assert (create.method, create.url.path) == ("POST", "/api/v1/hermes_messages")
    assert create.headers["Authorization"] == "Bearer relay-tok"
    assert b'"kind":"reply"' in create.content and b'"reply_to":"m-7"' in create.content
    assert (edit.method, edit.url.path) == ("PATCH", "/api/v1/hermes_messages/42")
    assert b'"final":true' in edit.content


@pytest.mark.asyncio
async def test_a_cron_send_is_filed_as_a_delivery():
    adapter, recorder = _adapter(), _Recorder()
    with _patched_client(recorder):
        await adapter.send("1", "Morning brief", metadata={"job_id": "c912b0758feb"})
    assert b'"kind":"delivery"' in recorder.requests[0].content


@pytest.mark.asyncio
async def test_approval_buttons_resolve_once_through_the_gateway_resolver():
    adapter, recorder = _adapter(), _Recorder()
    with _patched_client(recorder):
        result = await adapter.send_exec_approval("seed:1", "rm -rf build", "session-key-1", description="delete files")
    assert result.success
    body = recorder.requests[0].content.decode()
    approval_id = next(iter(adapter._approvals))
    assert f"appr:{approval_id}:approve" in body and '"kind":"approval"' in body

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
            ok = await cli.post("/actions", json={"action": f"appr:{approval_id}:approve"},
                                headers={"X-Seed-Secret": "s3cret"})
            again = await cli.post("/actions", json={"action": f"appr:{approval_id}:approve"},
                                   headers={"X-Seed-Secret": "s3cret"})
    assert (ok.status, again.status) == (200, 409)
    resolve.assert_called_once_with("session-key-1", "approve")


@pytest.mark.asyncio
async def test_standalone_send_files_cron_output_as_a_delivery(monkeypatch):
    monkeypatch.setenv("SEED_HOME_CHANNEL", "seed:1")
    recorder = _Recorder()
    with _patched_client(recorder):
        result = await _seed._standalone_send(PlatformConfig(enabled=True, extra=dict(EXTRA)), "", "Morning brief")
    assert result == {"success": True, "platform": "seed", "chat_id": "seed:1", "message_id": "42"}
    assert b'"kind":"delivery"' in recorder.requests[0].content


@pytest.mark.asyncio
async def test_seed_errors_become_failed_send_results():
    adapter = _adapter()
    with _patched_client(_Recorder(status=500)):
        result = await adapter.send("seed:1", "hello")
    assert result.success is False and "500" in result.error
