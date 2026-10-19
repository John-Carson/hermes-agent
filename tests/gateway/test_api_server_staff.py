"""Tests for the staff persona endpoints (GET /api/staff, PUT /api/staff/{name})."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware
from hermes_constants import get_hermes_home

MAIL = "---\nname: mail\ndescription: Read-only mail clerk.\n---\n\n# Mail\n\nBrief unread mail.\n"


def _app(api_key=""):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": api_key} if api_key else {}))
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/staff", adapter._handle_list_staff)
    app.router.add_put("/api/staff/{name}", adapter._handle_update_staff)
    return app


@pytest.fixture
def mail_skill():
    path = get_hermes_home() / "skills" / "staff" / "mail" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(MAIL)
    return path


async def _client(api_key=""):
    client = TestClient(TestServer(_app(api_key)))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_lists_each_persona_with_content_and_state(mail_skill):
    client = await _client()
    try:
        resp = await client.get("/api/staff")
        assert resp.status == 200
        assert (await resp.json())["staff"] == [
            {"name": "mail", "description": "Read-only mail clerk.", "content": MAIL, "enabled": True}
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_saves_content_and_turns_a_persona_off_and_on(mail_skill):
    client = await _client()
    edited = MAIL.replace("Brief unread mail.", "Brief unread mail twice a day.")
    try:
        resp = await client.put("/api/staff/mail", json={"content": edited, "enabled": False})
        assert resp.status == 200
        assert (await resp.json())["persona"]["enabled"] is False
        assert mail_skill.read_text() == edited

        from hermes_cli.config import load_config
        assert "mail" in load_config()["skills"]["disabled"]

        resp = await client.put("/api/staff/mail", json={"enabled": True})
        assert (await resp.json())["persona"]["enabled"] is True
        assert "mail" not in load_config()["skills"]["disabled"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "no frontmatter at all",
    "---\nname: finance\ndescription: x\n---\n\nbody\n",
    "---\nname: mail\n---\n\nbody\n",
])
async def test_refuses_content_that_is_not_this_persona(mail_skill, content):
    client = await _client()
    try:
        resp = await client.put("/api/staff/mail", json={"content": content})
        assert resp.status == 400
        assert mail_skill.read_text() == MAIL
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["finance", "..", ".hidden"])
async def test_unknown_or_unsafe_names_are_not_found(mail_skill, name):
    client = await _client()
    try:
        resp = await client.put(f"/api/staff/{name}", json={"enabled": False})
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_needs_the_api_key(mail_skill):
    client = await _client(api_key="sk-secret")
    try:
        assert (await client.get("/api/staff")).status == 401
        assert (await client.put("/api/staff/mail", json={"enabled": False})).status == 401
        resp = await client.get("/api/staff", headers={"Authorization": "Bearer sk-secret"})
        assert resp.status == 200
    finally:
        await client.close()
