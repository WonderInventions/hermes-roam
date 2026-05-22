"""Roam (ro.am) platform adapter for Hermes Agent.

A bundled platform plugin that runs an aiohttp webhook server, accepts
Roam ``chat.message`` webhooks (Standard-Webhooks signature-verified),
and relays messages to/from the agent via the standard
``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Threads are first-class.** Roam exposes a microsecond ``threadTimestamp``
on every threaded message. We round-trip it through Hermes'
``SessionSource.thread_id`` so replies land in the same thread as the
inbound. ``create_handoff_thread`` posts a seed message and returns its
timestamp so the gateway's handoff watcher gets a thread it can pin a
delegated session to.

**Edit-in-place streaming.** Roam's ``chat.post`` returns a timestamp
that doubles as the message identifier on ``chat.update``. We surface it
as ``SendResult.message_id`` so the gateway's stream consumer can edit
the original message rather than appending new bubbles. Optional native
streaming (``ROAM_NATIVE_STREAMING=true``) uses ``chat.startStream`` /
``chat.appendStream`` / ``chat.stopStream`` for cleaner UI on the Roam
client side.

**Allowlist gating.** Separate allowlists for users (DM senders) and
groups (chat UUIDs). ``ROAM_ALLOW_ALL_USERS=true`` is a dev-only escape
hatch.

**Mention gating in groups.** ``ROAM_REQUIRE_MENTION=true`` (default)
silences the bot in groups unless its UUID appears in a ``<@…>``
mention. We strip the bot's own mention from the inbound text before
handing it to the agent so the LLM doesn't see its own ID.

**Inbound dedupe.** Roam can double-deliver chat.message webhooks
~50ms apart with distinct ``webhook-id`` headers but identical
``chatId:timestamp`` payload. We dedupe on both keys with a bounded LRU.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
)
from gateway.config import Platform
from gateway.session import SessionSource

from .roam_client import (
    DEFAULT_API_BASE_URL,
    MAX_MESSAGE_TEXT_SIZE,
    RoamAPIError,
    RoamClient,
)
from .standard_webhooks import verify_signature

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_PORT = 8647
DEFAULT_WEBHOOK_PATH = "/roam/webhook"
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB — webhooks are tiny JSON

# Roam's 8000-byte text cap is enforced server-side. We surface a slightly
# lower max_message_length on registration so the chunker leaves headroom
# for multi-byte UTF-8.
DEFAULT_MAX_MESSAGE_LENGTH = 7500

# Bounded LRU sizes for inbound dedupe.
DEDUPE_MAX_ENTRIES = 2000

# Bot mention pattern used by Roam clients: <@uuid> or <!@uuid>.
_BOT_MENTION_RE = re.compile(r"<!?@([0-9a-f-]+)>", re.IGNORECASE)

# Fenced code-block delimiter — ``` or ~~~ with up to 3 leading spaces, per
# the CommonMark spec.
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def expand_soft_breaks(text: str) -> str:
    """Insert blank lines between non-empty consecutive lines outside code fences.

    Roam's Markdown renderer is CommonMark-strict, so a single newline
    between two non-empty lines is a soft break that collapses to a space
    at render time. The agent emits multi-line content (help tables,
    bulleted lists, paragraphs) with single newlines and expects each
    line to render on its own — so we forcibly upgrade single newlines
    to paragraph breaks. Fenced code blocks are passed through unchanged
    so command snippets render correctly.
    """
    if not text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        out.append(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if i + 1 >= len(lines) or in_fence:
            continue
        if line.strip() and lines[i + 1].strip():
            out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _truthy_env(name: str, default: bool = False) -> bool:
    return _truthy(os.getenv(name), default)


# ---------------------------------------------------------------------------
# Inbound dedupe
# ---------------------------------------------------------------------------

class _Deduplicator:
    """Bounded LRU cache for at-least-once webhook delivery dedupe."""

    def __init__(self, max_size: int = DEDUPE_MAX_ENTRIES) -> None:
        self._seen: Dict[str, float] = {}
        self._max = max_size

    def is_duplicate(self, key: str) -> bool:
        if not key:
            return False
        if key in self._seen:
            return True
        if len(self._seen) >= self._max:
            cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[key] = time.time()
        return False


# ---------------------------------------------------------------------------
# Webhook event parsing
# ---------------------------------------------------------------------------

def _strip_bot_mention(text: str, bot_id: Optional[str]) -> str:
    """Strip the bot's own ``<@uuid>`` mention from inbound text."""
    if not text:
        return text
    if bot_id:
        escaped = re.escape(bot_id)
        text = re.sub(rf"<!?@{escaped}>", "", text, flags=re.IGNORECASE)
    return text.strip()


def _was_bot_mentioned(text: str, bot_id: Optional[str]) -> bool:
    """Return True if the bot's UUID appears in a ``<@uuid>`` mention."""
    if not bot_id or not text:
        return False
    for match in _BOT_MENTION_RE.finditer(text):
        if match.group(1).lower() == bot_id.lower():
            return True
    return False


def _chat_type_to_hermes(roam_chat_type: str) -> str:
    """Map Roam's chatType to Hermes chat_type vocabulary.

    Roam emits ``"dm"`` for direct messages and ``"channel"`` (V0) or
    ``"group"`` (V1) for group chats. Hermes uses ``"dm"`` / ``"group"``.
    """
    if roam_chat_type == "dm":
        return "dm"
    return "group"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class RoamAdapter(BasePlatformAdapter):
    """Roam (ro.am) gateway adapter."""

    def __init__(self, config, **kwargs):
        platform = Platform("roam")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Credentials
        self.api_key: str = (
            os.getenv("ROAM_API_KEY") or extra.get("api_key", "")
        ).strip()
        self.webhook_secret: str = (
            os.getenv("ROAM_WEBHOOK_SECRET") or extra.get("webhook_secret", "")
        ).strip()

        # API base
        self.api_base_url: str = (
            os.getenv("ROAM_API_BASE_URL")
            or extra.get("api_base_url", "")
            or DEFAULT_API_BASE_URL
        ).rstrip("/")

        # Webhook server
        self.webhook_host: str = (
            os.getenv("ROAM_WEBHOOK_HOST") or extra.get("host", "0.0.0.0")
        )
        try:
            self.webhook_port: int = int(
                os.getenv("ROAM_WEBHOOK_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path: str = (
            os.getenv("ROAM_WEBHOOK_PATH") or extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )
        self.public_url: str = (
            os.getenv("ROAM_WEBHOOK_PUBLIC_URL")
            or extra.get("public_url", "")
            or ""
        ).rstrip("/")

        # Auth gating
        self.allow_all: bool = _truthy_env(
            "ROAM_ALLOW_ALL_USERS",
            bool(extra.get("allow_all_users", False)),
        )
        self.allowed_users: Set[str] = _csv_set(
            os.getenv("ROAM_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))
        self.allowed_groups: Set[str] = _csv_set(
            os.getenv("ROAM_ALLOWED_GROUPS", "")
        ) | set(extra.get("allowed_groups", []))

        # Behavior
        self.require_mention: bool = _truthy_env(
            "ROAM_REQUIRE_MENTION",
            bool(extra.get("require_mention", True)),
        )
        self.native_streaming: bool = _truthy_env(
            "ROAM_NATIVE_STREAMING",
            bool(extra.get("native_streaming", False)),
        )
        # When true, top-level inbound messages get a thread_id anchored
        # on the inbound message's own timestamp, so the bot's reply
        # creates a thread off the post rather than landing back in the
        # main channel. Idiomatic for org bots that idle in busy channels.
        self.reply_in_thread: bool = _truthy_env(
            "ROAM_REPLY_IN_THREAD",
            bool(extra.get("reply_in_thread", False)),
        )
        self.sender_id: Optional[str] = (
            os.getenv("ROAM_SENDER_ID") or extra.get("sender_id") or None
        )

        # Runtime state
        self._client: Optional[RoamClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._bot_user_id: Optional[str] = None
        self._bot_name: Optional[str] = None
        # For PATs: the human owner's identity, surfaced as user_name on
        # inbound messages so the agent refers to them by name.
        self._owner_id: Optional[str] = None
        self._owner_name: Optional[str] = None
        self._owner_email: Optional[str] = None
        self._dedup_webhook_id = _Deduplicator()
        self._dedup_message = _Deduplicator()
        self._lock_key: Optional[str] = None
        self._subscribed_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self.api_key or not self.webhook_secret:
            self._set_fatal_error(
                "config_missing",
                "ROAM_API_KEY and ROAM_WEBHOOK_SECRET must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from sharing the same API key.
        try:
            from gateway.status import acquire_scoped_lock
            tok_hash = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
            if not acquire_scoped_lock("roam", tok_hash):
                self._set_fatal_error(
                    "lock_conflict",
                    "Roam API key already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None

        self._client = RoamClient(
            self.api_key,
            base_url=self.api_base_url,
            default_sender_id=self.sender_id,
        )

        # Best-effort: fetch identity info via token.info.
        #
        # For a personal access token (PAT), the response includes both:
        #   user: the human owner ({id, name, email?, imageUrl?})
        #   bot : the persona the API key posts as ({id, name, imageUrl?})
        # For an org token, only ``user`` is set and it IS the bot.
        #
        # We use the bot id for self-echo filtering and mention detection,
        # and the owner id/name/email to give the agent a real human
        # identity when the PAT owner DMs the bot.
        try:
            info = await self._client.token_info()
            self._apply_token_info(info)
        except Exception as exc:
            logger.debug("roam: token.info failed: %s", exc)

        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "missing_dep",
                "aiohttp is required for the Roam adapter — install with `pip install aiohttp`",
                retryable=False,
            )
            return False

        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        self._app.router.add_get(f"{self.webhook_path}/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Could not bind Roam webhook on {self.webhook_host}:{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        # Auto-subscribe if we have a public URL. Best-effort — if the
        # subscription already exists Roam returns 2xx; if it fails we log
        # but keep the adapter running so manual setup can complete.
        if self.public_url:
            target_url = f"{self.public_url}{self.webhook_path}"
            try:
                await self._client.webhook_subscribe(target_url)
                self._subscribed_url = target_url
                logger.info("roam: subscribed to chat.message webhooks at %s", target_url)
            except Exception as exc:
                logger.warning("roam: webhook.subscribe failed: %s", exc)

        self._mark_connected()
        logger.info(
            "roam: webhook listening on %s:%s%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
            f" (public: {self.public_url})" if self.public_url else "",
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

        if self._client and self._subscribed_url:
            try:
                await self._client.webhook_unsubscribe(self._subscribed_url)
            except Exception as exc:
                logger.debug("roam: webhook.unsubscribe failed: %s", exc)
            self._subscribed_url = None

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("roam", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    def _apply_token_info(self, info: Dict[str, Any]) -> None:
        """Populate bot + owner identity from a ``/v1/token.info`` response.

        Shape (V1):
            {
              "user": {"id","name","email?","imageUrl?"},
              "bot":  {"id","name","imageUrl?"},  # PAT only
              "scopes": [...],
              "roam":  {...}
            }
        """
        user = info.get("user") or {}
        bot = info.get("bot") or {}
        if bot:
            # Personal access token: the API key acts on behalf of a person.
            self._bot_user_id = bot.get("id") or None
            self._bot_name = bot.get("name") or None
            self._owner_id = user.get("id") or None
            self._owner_name = user.get("name") or None
            self._owner_email = user.get("email") or None
            logger.info(
                "roam: PAT owner=%s (id=%s); bot persona=%s (id=%s)",
                self._owner_name or "?", self._owner_id or "?",
                self._bot_name or "?", self._bot_user_id or "?",
            )
        else:
            # Org token: user IS the bot.
            self._bot_user_id = user.get("id") or None
            self._bot_name = user.get("name") or None
            self._owner_id = None
            self._owner_name = None
            self._owner_email = None
            logger.info(
                "roam: org token; bot=%s (id=%s)",
                self._bot_name or "?", self._bot_user_id or "?",
            )

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "roam"})

    async def _handle_webhook(self, request) -> Any:
        from aiohttp import web

        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("roam: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        if not verify_signature(self.webhook_secret, request.headers, body):
            return web.Response(status=401, text="invalid webhook signature")

        try:
            import json as _json
            payload = _json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return web.Response(status=400, text="bad json")

        # Dedupe on webhook-id first — covers Standard-Webhooks redeliveries.
        webhook_id = request.headers.get("webhook-id") or ""
        if webhook_id and self._dedup_webhook_id.is_duplicate(webhook_id):
            return web.Response(status=200, text="{}")

        # Acknowledge immediately, dispatch in the background. Roam's
        # delivery deadline is short — we don't want LLM latency to leak
        # into webhook ack timing.
        asyncio.create_task(self._dispatch_event_safe(payload))
        return web.Response(status=200, text="{}", content_type="application/json")

    async def _dispatch_event_safe(self, payload: Dict[str, Any]) -> None:
        try:
            await self._dispatch_event(payload)
        except Exception:
            logger.exception("roam: dispatch_event failed")

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") != "message":
            logger.debug("roam: ignoring event type %r", event.get("type"))
            return

        chat_id = event.get("chatId") or ""
        user_id = event.get("userId") or ""
        roam_chat_type = event.get("chatType") or ""
        timestamp = event.get("timestamp")
        thread_timestamp = event.get("threadTimestamp")
        message_id = event.get("messageId") or (str(timestamp) if timestamp else "")
        text = event.get("text") or ""
        items = event.get("items") or []

        if not chat_id or not user_id or not timestamp:
            logger.debug("roam: dropping malformed event: %s", event)
            return

        # Per-message dedup as a fallback when webhook-id is missing or
        # Roam reuses the same payload across distinct envelopes.
        msg_key = f"{chat_id}:{timestamp}"
        if self._dedup_message.is_duplicate(msg_key):
            return

        # Self-echo filter.
        if self._bot_user_id and user_id == self._bot_user_id:
            return

        # Allowlist gate.
        hermes_chat_type = _chat_type_to_hermes(roam_chat_type)
        if not self._allowed(hermes_chat_type, chat_id, user_id):
            logger.info(
                "roam: rejecting unauthorized message chat_type=%s chat_id=%s user_id=%s",
                hermes_chat_type, chat_id, user_id,
            )
            return

        # Mention gate for groups.
        was_mentioned = _was_bot_mentioned(text, self._bot_user_id)
        if hermes_chat_type == "group" and self.require_mention and not was_mentioned:
            return

        cleaned_text = _strip_bot_mention(text, self._bot_user_id)

        # Media handling: cache the item URLs into Hermes' image cache so the
        # vision pipeline can find them on disk. Failures are skipped, not
        # fatal — the agent still gets the text.
        media_urls: List[str] = []
        media_types: List[str] = []
        if items:
            await self._collect_media(items, media_urls, media_types)

        # Map threadTimestamp to Hermes thread_id so outbound replies route
        # back to the same Roam-native thread. When ROAM_REPLY_IN_THREAD is
        # set and the inbound was top-level, anchor a new thread on the
        # inbound message itself so the bot's reply doesn't pollute the
        # main channel.
        if thread_timestamp:
            thread_id = str(thread_timestamp)
        elif self.reply_in_thread:
            thread_id = str(timestamp)
        else:
            thread_id = None

        # Resolve a human-readable user_name when token.info told us who the
        # PAT owner is. Without this the agent only sees a UUID and has no
        # way to refer to the user by name. For non-owner senders (e.g.
        # group chat participants) we don't have name info — fall back to
        # the UUID.
        is_owner = bool(self._owner_id and user_id == self._owner_id)
        user_name = self._owner_name if (is_owner and self._owner_name) else user_id

        # Inject owner email into the per-message channel context so the
        # agent can address Rob by email when appropriate (e.g. drafting
        # calendar invites). Only set when we actually have it AND the
        # sender is the owner — we don't leak owner email to other senders.
        channel_context = None
        if is_owner and self._owner_email:
            channel_context = f"Owner email: {self._owner_email}"

        source = self.build_source(
            chat_id=chat_id,
            chat_type=hermes_chat_type,
            user_id=user_id,
            user_name=user_name,
            chat_name=chat_id,
            thread_id=thread_id,
            message_id=message_id,
        )

        message_type = MessageType.TEXT
        if media_urls and not cleaned_text:
            message_type = MessageType.PHOTO

        event_obj = MessageEvent(
            text=cleaned_text,
            message_type=message_type,
            source=source,
            raw_message=event,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            channel_context=channel_context,
        )

        await self.handle_message(event_obj)

    def _allowed(self, chat_type: str, chat_id: str, user_id: str) -> bool:
        if self.allow_all:
            return True
        if chat_type == "dm":
            return bool(user_id) and user_id in self.allowed_users
        if chat_type == "group":
            return bool(chat_id) and chat_id in self.allowed_groups
        return False

    async def _collect_media(
        self,
        items: List[Dict[str, Any]],
        media_urls: List[str],
        media_types: List[str],
    ) -> None:
        """Download item URLs into Hermes' media cache.

        Roam webhook items carry a public ``url`` field. We fetch the bytes
        with the bot's API key and stash them in the local cache so the
        agent's vision tools can read them as file paths.
        """
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=30.0)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for item in items:
                url = item.get("url")
                mime = item.get("mime") or "application/octet-stream"
                if not url:
                    continue
                try:
                    async with session.get(
                        url, headers={"Authorization": f"Bearer {self.api_key}"}
                    ) as resp:
                        if resp.status >= 400:
                            logger.debug("roam: fetch media %s -> %d", url, resp.status)
                            continue
                        data = await resp.read()
                except Exception as exc:
                    logger.debug("roam: fetch media %s failed: %s", url, exc)
                    continue
                ext = self._ext_from_mime(mime, item.get("name"))
                try:
                    local_path = cache_image_from_bytes(data, ext=ext)
                except Exception as exc:
                    logger.debug("roam: cache media failed: %s", exc)
                    continue
                media_urls.append(local_path)
                media_types.append(mime)

    @staticmethod
    def _ext_from_mime(mime: str, name: Optional[str]) -> str:
        # Prefer the file's actual extension when known.
        if name and "." in name:
            return "." + name.rsplit(".", 1)[-1].lower()
        if "/" in mime:
            sub = mime.split("/", 1)[1].lower()
            mapping = {"jpeg": ".jpg"}
            return "." + mapping.get(sub, sub)
        return ".bin"

    # ------------------------------------------------------------------
    # Outbound — send / edit / typing
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Roam adapter not connected")

        # Turn-based chat already implies "this is a reply to the previous
        # turn", and Roam renders replyTimestamp as a visible quote block
        # in the message. We intentionally drop the gateway's auto-supplied
        # reply_to — only an explicit metadata override (referencing an
        # older, non-most-recent message) sets it.
        thread_ts = _thread_ts_from_metadata(metadata)
        reply_ts: Optional[int] = _reply_ts_from_metadata(metadata)

        try:
            data = await self._client.chat_post(
                chat_id=chat_id,
                text=expand_soft_breaks(content),
                thread_timestamp=thread_ts,
                reply_to=reply_ts,
            )
        except RoamAPIError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.status >= 500,
            )
        except Exception as exc:
            logger.exception("roam: chat.post failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        ts = data.get("timestamp")
        return SendResult(
            success=True,
            message_id=str(ts) if ts is not None else None,
            raw_response=data,
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Roam adapter not connected")
        try:
            timestamp = int(message_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error=f"Roam edit requires int timestamp, got {message_id!r}")

        try:
            data = await self._client.chat_update(
                chat_id=chat_id,
                timestamp=timestamp,
                text=expand_soft_breaks(content),
            )
        except RoamAPIError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.status >= 500,
            )
        except Exception as exc:
            logger.exception("roam: chat.update failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        ts = data.get("timestamp", timestamp)
        return SendResult(
            success=True,
            message_id=str(ts),
            raw_response=data,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if not self._client or not chat_id:
            return
        try:
            await self._client.chat_typing(
                chat_id=chat_id,
                thread_timestamp=_thread_ts_from_metadata(metadata),
            )
        except Exception as exc:
            # Typing failures are non-fatal — never let them break the run.
            logger.debug("roam: chat.typing failed: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Best-effort chat info.

        Roam doesn't expose a per-chat metadata endpoint that's safe to call
        on every inbound, so we return the chat_id as the name and leave
        type as ``dm`` (the gateway only relies on ``name`` and ``type``).
        Concrete chat_type is already carried on ``SessionSource`` from the
        webhook event, which is the authoritative source.
        """
        return {"name": chat_id or "", "type": "dm"}

    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Open a fresh Roam thread by posting a seed message.

        We use ``sync: true`` so the response carries the
        ``timestamp`` we use as the thread anchor. Subsequent messages
        sent with ``threadTimestamp=<this value>`` land in the new thread.
        """
        if not self._client:
            return None
        seed = name.strip() or "New thread"
        try:
            data = await self._client.chat_post(
                chat_id=parent_chat_id,
                text=seed,
            )
        except Exception as exc:
            logger.warning("roam: create_handoff_thread failed: %s", exc)
            return None
        ts = data.get("timestamp")
        return str(ts) if ts is not None else None


def _thread_ts_from_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[int]:
    """Extract a Roam ``threadTimestamp`` (int microseconds) from metadata.

    The gateway passes a metadata dict to ``send`` / ``send_typing``.
    We honor both ``thread_timestamp`` (raw int) and ``thread_id``
    (str-encoded int from ``SessionSource.thread_id``).
    """
    if not metadata:
        return None
    for key in ("thread_timestamp", "threadTimestamp", "thread_id", "threadId"):
        value = metadata.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _reply_ts_from_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[int]:
    """Extract a Roam ``replyTimestamp`` (int microseconds) from metadata.

    Only honored when the caller explicitly opts in — the gateway's
    routine ``reply_to`` (which always points at the previous inbound) is
    intentionally ignored, since Roam already renders turn-based replies
    implicitly. An explicit ``reply_to_timestamp`` metadata key is the
    escape hatch for referencing an older message.
    """
    if not metadata:
        return None
    for key in ("reply_to_timestamp", "replyToTimestamp", "replyTimestamp"):
        value = metadata.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    if not os.getenv("ROAM_API_KEY"):
        return False
    if not os.getenv("ROAM_WEBHOOK_SECRET"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_key = bool(os.getenv("ROAM_API_KEY") or extra.get("api_key"))
    has_secret = bool(os.getenv("ROAM_WEBHOOK_SECRET") or extra.get("webhook_secret"))
    return has_key and has_secret


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from env-only setups."""
    if not (os.getenv("ROAM_API_KEY") and os.getenv("ROAM_WEBHOOK_SECRET")):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("ROAM_API_BASE_URL"):
        seeded["api_base_url"] = os.environ["ROAM_API_BASE_URL"]
    if os.getenv("ROAM_WEBHOOK_HOST"):
        seeded["host"] = os.environ["ROAM_WEBHOOK_HOST"]
    if os.getenv("ROAM_WEBHOOK_PORT"):
        try:
            seeded["port"] = int(os.environ["ROAM_WEBHOOK_PORT"])
        except ValueError:
            pass
    if os.getenv("ROAM_WEBHOOK_PATH"):
        seeded["webhook_path"] = os.environ["ROAM_WEBHOOK_PATH"]
    if os.getenv("ROAM_WEBHOOK_PUBLIC_URL"):
        seeded["public_url"] = os.environ["ROAM_WEBHOOK_PUBLIC_URL"]
    if os.getenv("ROAM_HOME_CHANNEL"):
        seeded["home_channel"] = {
            "chat_id": os.environ["ROAM_HOME_CHANNEL"],
            "name": "Home",
        }
    return seeded


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process delivery for cron jobs running detached from the gateway.

    Opens an ephemeral RoamClient, posts the message, and returns. Media
    files are not deliverable from this path yet (Phase 2: item.upload)
    so we append a text hint listing how many were generated.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    api_key = (os.getenv("ROAM_API_KEY") or extra.get("api_key", "")).strip()
    if not api_key or not chat_id:
        return {"error": "Roam standalone send: missing api_key or chat_id"}

    api_base = (
        os.getenv("ROAM_API_BASE_URL")
        or extra.get("api_base_url", "")
        or DEFAULT_API_BASE_URL
    )
    sender_id = os.getenv("ROAM_SENDER_ID") or extra.get("sender_id")
    client = RoamClient(api_key, base_url=api_base, default_sender_id=sender_id)

    text = message or ""
    if media_files:
        text = (
            text
            + (f"\n\n[{len(media_files)} attachment(s) generated; not deliverable from cron]")
        )
    if len(text) > MAX_MESSAGE_TEXT_SIZE:
        text = text[: MAX_MESSAGE_TEXT_SIZE - 1] + "…"

    thread_ts: Optional[int] = None
    if thread_id:
        try:
            thread_ts = int(thread_id)
        except (TypeError, ValueError):
            thread_ts = None

    try:
        data = await client.chat_post(
            chat_id=chat_id,
            text=text,
            thread_timestamp=thread_ts,
        )
    except RoamAPIError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}

    ts = data.get("timestamp")
    return {
        "success": True,
        "message_id": str(ts) if ts is not None else None,
    }


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="roam",
        label="Roam",
        adapter_factory=lambda cfg: RoamAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ROAM_API_KEY", "ROAM_WEBHOOK_SECRET"],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ROAM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ROAM_ALLOWED_USERS",
        allow_all_env="ROAM_ALLOW_ALL_USERS",
        max_message_length=DEFAULT_MAX_MESSAGE_LENGTH,
        emoji="🔵",
        platform_hint=(
            "You are chatting via Roam (ro.am). Roam renders Markdown — "
            "bold, italics, code blocks, and links all work. Replies in a "
            "thread should stay in the thread; the adapter routes them via "
            "threadTimestamp automatically. Each message is capped at 8000 "
            "bytes; the chunker uses 7500 to leave UTF-8 headroom. Edits to "
            "previously sent messages work in-place (chat.update)."
        ),
    )
