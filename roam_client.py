"""Thin async HTTP client for the Roam (ro.am) V1 API.

We hit aiohttp directly rather than depending on a vendor SDK — the
endpoints we call (chat.post, chat.update, chat.typing, the streaming
trio, plus webhook.subscribe/unsubscribe and token.info) are stable JSON
POSTs and the gain from a wrapper would be marginal.

All endpoints are POSTs with ``Authorization: Bearer <ROAM_API_KEY>``
and ``Content-Type: application/json``. The base URL is configurable
via ``ROAM_API_BASE_URL`` and defaults to ``https://api.ro.am/v1``.

Roam's API uses bare UUIDs for chat/user identifiers and
microsecond-precision integer timestamps. ``chat.post`` returns
``{chatId, timestamp, threadTimestamp}``; the timestamp doubles as the
message identifier on chat.update.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


DEFAULT_API_BASE_URL = "https://api.ro.am/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Roam enforces an 8000-byte cap per chat.post message and per chat.stream
# snapshot. We surface a slightly lower default to leave headroom for the
# JSON envelope overhead when callers measure in characters.
MAX_MESSAGE_TEXT_SIZE = 8000


def _read_plugin_version() -> str:
    """Return this plugin's version, read from the sibling ``plugin.yaml``.

    ``plugin.yaml`` is the one manifest present under both install layouts — the
    Git-URL clone and the release tarball (which stages plugin.yaml + the ``.py``
    files, but not pyproject.toml). Parsed with a small regex so we don't depend
    on PyYAML, which isn't a runtime dependency. Falls back to a sentinel so a
    missing/garbled manifest never breaks request sending.
    """
    manifest = Path(__file__).resolve().parent / "plugin.yaml"
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            m = re.match(r"""version:\s*["']?([^"'\s#]+)""", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return "0+unknown"


PLUGIN_VERSION = _read_plugin_version()

# Advertised to the Roam appserver on every request: identifies this plugin and
# version for attribution in logs and Datadog (@plugin.name / @plugin.version).
USER_AGENT = f"hermes-roam/{PLUGIN_VERSION}"

# The Roam API version this plugin is built and tested against, pinned via the
# ``Roam-Version`` header (and ``version`` on webhook.subscribe) so future /v1/
# shape changes don't reach the plugin until it's bumped in lockstep with the
# parsing code.
ROAM_API_VERSION = "2026-06-01"


class RoamAPIError(RuntimeError):
    """Raised when the Roam API returns a non-2xx response.

    ``status`` is the HTTP status code; ``body`` is the response body
    (truncated). Callers map common codes to user-facing messages.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Roam API {status}: {body[:200]}")
        self.status = status
        self.body = body


class RoamClient:
    """Async HTTP client for the Roam V1 API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_sender_id: Optional[str] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._default_sender_id = default_sender_id

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Roam-Version": ROAM_API_VERSION,
        }

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import aiohttp

        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, headers=self._headers(), json=body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RoamAPIError(resp.status, text)
                if not text:
                    return {}
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    # The server returned a non-JSON 2xx — surface as empty dict
                    # rather than failing, since we already have the status.
                    return {}

    async def _get(self, path: str) -> Dict[str, Any]:
        import aiohttp

        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RoamAPIError(resp.status, text)
                if not text:
                    return {}
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return {}

    def _apply_sender(self, body: Dict[str, Any], sender_id: Optional[str]) -> None:
        sid = sender_id if sender_id is not None else self._default_sender_id
        if sid:
            body["sender"] = {"id": sid}

    async def chat_post(
        self,
        chat_id: str,
        text: str,
        *,
        thread_timestamp: Optional[int] = None,
        reply_to: Optional[int] = None,
        markdown: bool = True,
        sync: bool = True,
        items: Optional[List[str]] = None,
        sender_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "chatId": chat_id,
            "text": text,
            "markdown": markdown,
            "sync": sync,
        }
        if thread_timestamp is not None:
            body["threadTimestamp"] = int(thread_timestamp)
        if reply_to is not None:
            body["replyTo"] = int(reply_to)
        if items:
            body["items"] = list(items)
        self._apply_sender(body, sender_id)
        return await self._post("/chat.post", body)

    async def chat_update(
        self,
        chat_id: str,
        timestamp: int,
        text: str,
        *,
        thread_timestamp: Optional[int] = None,
        markdown: bool = True,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "chatId": chat_id,
            "timestamp": int(timestamp),
            "text": text,
            "markdown": markdown,
        }
        if thread_timestamp is not None:
            body["threadTimestamp"] = int(thread_timestamp)
        return await self._post("/chat.update", body)

    async def chat_typing(
        self,
        chat_id: str,
        *,
        thread_timestamp: Optional[int] = None,
        sender_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"chatId": chat_id}
        if thread_timestamp is not None:
            body["threadTimestamp"] = int(thread_timestamp)
        # chat.typing accepts sender.id only (no name/imageUrl).
        sid = sender_id if sender_id is not None else self._default_sender_id
        if sid:
            body["sender"] = {"id": sid}
        return await self._post("/chat.typing", body)

    async def chat_start_stream(
        self,
        chat_id: str,
        *,
        text: str = "",
        kind: str = "text",
        thread_timestamp: Optional[int] = None,
        sender_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "chatId": chat_id,
            "kind": kind,
        }
        if text:
            body["text"] = text
        if thread_timestamp is not None:
            body["threadTimestamp"] = int(thread_timestamp)
        self._apply_sender(body, sender_id)
        return await self._post("/chat.startStream", body)

    async def chat_append_stream(
        self,
        stream_id: str,
        text: str,
        *,
        snapshot: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "streamId": stream_id,
            "text": text,
            "snapshot": bool(snapshot),
        }
        return await self._post("/chat.appendStream", body)

    async def chat_stop_stream(
        self,
        stream_id: str,
        *,
        text: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"streamId": stream_id}
        if text:
            body["text"] = text
        return await self._post("/chat.stopStream", body)

    async def webhook_subscribe(self, url: str, event: str = "chat.message") -> Dict[str, Any]:
        """Subscribe ``url`` to ``event`` and return the subscription record.

        The response carries the server-assigned ``id`` (a UUID) alongside the
        event/url/filter. That ``id`` — not url+event — is what
        :meth:`webhook_unsubscribe` keys on, so callers should retain it.
        Re-subscribing an existing url+event is idempotent server-side and
        returns the same record.
        """
        return await self._post(
            "/webhook.subscribe",
            {"url": url, "event": event, "version": ROAM_API_VERSION},
        )

    async def webhook_list(self) -> List[Dict[str, Any]]:
        """Return this API key's webhook subscriptions.

        ``GET /v1/webhook.list`` responds with ``{"webhooks": [{id, event,
        url, filter, dynamic, created}, ...]}`` scoped to the calling key, and
        requires the ``webhook:read`` scope. Useful for recovering a
        subscription ``id`` that wasn't retained from webhook.subscribe, or for
        sweeping up stale rows left by older clients.
        """
        data = await self._get("/webhook.list")
        return list(data.get("webhooks") or [])

    async def webhook_unsubscribe(self, webhook_id: str) -> None:
        """Delete a webhook subscription by its server-assigned ``id``.

        ``POST /v1/webhook.unsubscribe`` keys on the subscription ``id`` (a
        UUID), not on url+event — passing url+event matches nothing and the
        subscription is silently left in place. Obtain the ``id`` from the
        webhook.subscribe response (preferred) or :meth:`webhook_list`. The
        server returns 204 on success and 404 if the id is already gone.
        """
        if not webhook_id:
            return
        await self._post("/webhook.unsubscribe", {"id": webhook_id})

    async def token_info(self) -> Dict[str, Any]:
        """Fetch the bot persona identity bound to this API key."""
        return await self._get("/token.info")
