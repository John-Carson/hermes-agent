"""Seed platform adapter: Seed's web and iOS app is the chat client.

Outbound (Hermes -> Seed), Bearer ``SEED_RELAY_TOKEN``:
  POST  {SEED_URL}/api/v1/hermes_messages        {chat_id, kind, text, actions?, reply_to?} -> {id}
  PATCH {SEED_URL}/api/v1/hermes_messages/{id}   {text, final}

Inbound (Seed -> Hermes), header ``X-Seed-Secret: SEED_INBOUND_SECRET``:
  POST /messages  {chat_id, user_id, user_name, text, message_id}
  POST /actions   {chat_id, user_id, action}   action is ``appr:<id>:<choice>``

Identity: ``user_id`` is Seed's user ID. Seed authenticates the person before it calls
this adapter, and the shared secret proves the caller is Seed. Keep the listener on a
private network.
"""

import hmac
import logging
import os
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx
    from aiohttp import web

    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False
    httpx = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
MAX_BODY_BYTES = 256 * 1024
APPROVAL_STATE_LIMIT = 200
APPROVAL_CHOICES = ("approve", "deny")


def _setting(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    return str(extra.get(key) or os.getenv(env, default) or "").strip()


def check_requirements() -> bool:
    return DEPS_AVAILABLE and all(os.getenv(name, "").strip() for name in ("SEED_URL", "SEED_RELAY_TOKEN", "SEED_INBOUND_SECRET"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return all(_setting(extra, key, env) for key, env in (
        ("url", "SEED_URL"), ("relay_token", "SEED_RELAY_TOKEN"), ("inbound_secret", "SEED_INBOUND_SECRET")))


def is_connected(config) -> bool:
    return validate_config(config)


async def _post_message(url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{url}/api/v1/hermes_messages", json=payload,
                                 headers={"Authorization": f"Bearer {token}"})
    if resp.status_code >= 300:
        raise RuntimeError(f"Seed HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


class SeedAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config):
        from gateway.config import Platform

        super().__init__(config, Platform("seed"))
        extra = getattr(config, "extra", {}) or {}
        self._url = _setting(extra, "url", "SEED_URL").rstrip("/")
        self._token = _setting(extra, "relay_token", "SEED_RELAY_TOKEN")
        self._secret = _setting(extra, "inbound_secret", "SEED_INBOUND_SECRET")
        self._host = _setting(extra, "listen_host", "SEED_LISTEN_HOST", "0.0.0.0")
        self._port = int(_setting(extra, "listen_port", "SEED_LISTEN_PORT", "8650"))
        self._runner = None
        self._approvals: "OrderedDict[str, str]" = OrderedDict()

    # -- lifecycle -----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not DEPS_AVAILABLE or not (self._url and self._token and self._secret):
            logger.error("[seed] SEED_URL, SEED_RELAY_TOKEN, and SEED_INBOUND_SECRET are required")
            return False
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post("/messages", self._handle_inbound_message)
        app.router.add_post("/actions", self._handle_inbound_action)
        app.router.add_get("/health", lambda _request: web.json_response({"status": "ok"}))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        self._mark_connected()
        logger.info("[seed] listening on %s:%s, posting to %s", self._host, self._port, self._url)
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    # -- outbound ------------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        kind = "delivery" if (metadata or {}).get("job_id") else "reply"
        return await self._create(chat_id, kind, content, reply_to=reply_to)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.patch(f"{self._url}/api/v1/hermes_messages/{message_id}",
                                          json={"text": content, "final": finalize},
                                          headers={"Authorization": f"Bearer {self._token}"})
            if resp.status_code >= 300:
                return SendResult(success=False, error=f"Seed HTTP {resp.status_code}")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=f"Seed edit failed: {exc}")

    async def send_exec_approval(self, chat_id: str, command: str, session_key: str,
                                 description: str = "dangerous command",
                                 metadata: Optional[Dict[str, Any]] = None,
                                 allow_permanent: bool = True, smart_denied: bool = False) -> SendResult:
        del allow_permanent  # Seed offers one-shot Approve / Deny, like WhatsApp Cloud.
        approval_id = uuid.uuid4().hex[:12]
        text = f"Approval needed: {description}\n\n```\n{(command or '')[:1500]}\n```"
        if smart_denied:
            text += "\n\nApproving overrides a smart deny for this one operation only."
        actions = [{"id": f"appr:{approval_id}:{choice}", "label": choice.capitalize()} for choice in APPROVAL_CHOICES]
        result = await self._create(chat_id, "approval", text, actions=actions)
        if result.success:
            self._approvals[approval_id] = session_key
            while len(self._approvals) > APPROVAL_STATE_LIMIT:
                self._approvals.popitem(last=False)
        return result

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self.send(chat_id, f"{caption or ''}\n{image_url}".strip(), reply_to=reply_to)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Seed", "type": "dm", "chat_id": chat_id}

    async def _create(self, chat_id: str, kind: str, text: str, *, reply_to: Optional[str] = None,
                      actions: Optional[List[Dict[str, str]]] = None) -> SendResult:
        payload: Dict[str, Any] = {"chat_id": chat_id, "kind": kind, "text": text[:MAX_MESSAGE_LENGTH]}
        if reply_to:
            payload["reply_to"] = reply_to
        if actions:
            payload["actions"] = actions
        try:
            data = await _post_message(self._url, self._token, payload)
            return SendResult(success=True, message_id=str(data.get("id")))
        except Exception as exc:
            logger.warning("[seed] send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    # -- inbound -------------------------------------------------------------

    def _authorized(self, request) -> bool:
        given = request.headers.get("X-Seed-Secret", "")
        return bool(self._secret) and hmac.compare_digest(given.encode(), self._secret.encode())

    async def _read_json(self, request) -> Optional[Dict[str, Any]]:
        try:
            body = await request.json()
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    async def _handle_inbound_message(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._read_json(request)
        text = str((body or {}).get("text") or "").strip()
        chat_id = str((body or {}).get("chat_id") or "").strip()
        user_id = str((body or {}).get("user_id") or "").strip()
        if not (text and chat_id and user_id):
            return web.json_response({"error": "chat_id, user_id, and text are required"}, status=400)
        source = self.build_source(chat_id=chat_id, chat_name="Seed", chat_type="dm", user_id=user_id,
                                   user_name=str(body.get("user_name") or user_id))
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                             message_id=str(body.get("message_id") or uuid.uuid4().hex), raw_message=body,
                             timestamp=datetime.now(tz=timezone.utc))
        await self.handle_message(event)
        return web.json_response({"status": "accepted"}, status=202)

    async def _handle_inbound_action(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await self._read_json(request) or {}
        parts = str(body.get("action") or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "appr" or parts[2] not in APPROVAL_CHOICES:
            return web.json_response({"error": "unknown action"}, status=400)
        session_key = self._approvals.pop(parts[1], None)
        if session_key is None:
            return web.json_response({"error": "approval is stale or already answered"}, status=409)
        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(session_key, parts[2])
        return web.json_response({"status": "resolved", "waiters": resolved})


def _env_enablement() -> Optional[dict]:
    if not check_requirements():
        return None
    seed: Dict[str, Any] = {"url": os.getenv("SEED_URL", "").rstrip("/")}
    home = os.getenv("SEED_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Seed"}
    return seed


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[List[str]] = None, force_document: bool = False) -> Dict[str, Any]:
    """Cron delivery when the gateway is not in this process. Seed files it as a delivery."""
    extra = getattr(pconfig, "extra", {}) or {}
    url = _setting(extra, "url", "SEED_URL").rstrip("/")
    token = _setting(extra, "relay_token", "SEED_RELAY_TOKEN")
    target = chat_id or os.getenv("SEED_HOME_CHANNEL", "").strip()
    if not (DEPS_AVAILABLE and url and token and target):
        return {"error": "seed standalone send: SEED_URL, SEED_RELAY_TOKEN, and a chat ID are required"}
    try:
        data = await _post_message(url, token, {"chat_id": target, "kind": "delivery", "text": message[:MAX_MESSAGE_LENGTH]})
        return {"success": True, "platform": "seed", "chat_id": target, "message_id": str(data.get("id"))}
    except Exception as exc:
        return {"error": f"seed standalone send failed: {exc}"}


def register(ctx) -> None:
    ctx.register_platform(
        name="seed",
        label="Seed",
        adapter_factory=lambda cfg: SeedAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["SEED_URL", "SEED_RELAY_TOKEN", "SEED_INBOUND_SECRET"],
        install_hint="httpx and aiohttp ship with Hermes",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="SEED_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SEED_ALLOWED_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🌱",
        pii_safe=True,
        platform_hint=(
            "You are talking with Jack in Seed, his personal app (web and iPhone). "
            "Markdown renders. Your messages also arrive as phone notifications, so lead with the point."
        ),
    )
