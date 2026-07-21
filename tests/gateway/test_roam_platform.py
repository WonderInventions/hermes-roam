"""Tests for the Roam platform adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from roam.adapter import (
    RoamAdapter,
    _Deduplicator,
    _reply_ts_from_metadata,
    _strip_bot_mention,
    _thread_ts_from_metadata,
    _was_bot_mentioned,
    expand_soft_breaks,
    unwrap_webhook_envelope,
)
from roam.roam_client import RoamAPIError
from roam.standard_webhooks import verify_signature


# ---------------------------------------------------------------------------
# Standard Webhooks verification
# ---------------------------------------------------------------------------

SECRET_BYTES = b"swh-test-secret-bytes-for-hmac!!"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode("ascii")


def _sign(msg_id: str, ts: int, body: bytes, secret_bytes: bytes = SECRET_BYTES) -> str:
    signed = f"{msg_id}.{ts}.".encode("utf-8") + body
    digest = hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def test_verify_signature_accepts_valid_payload():
    body = b'{"type":"message"}'
    msg_id = "msg_01"
    ts = int(time.time())
    sig = _sign(msg_id, ts, body)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sig,
    }
    assert verify_signature(SECRET, headers, body) is True


def test_verify_signature_rejects_tampered_body():
    body = b'{"type":"message"}'
    msg_id = "msg_02"
    ts = int(time.time())
    sig = _sign(msg_id, ts, body)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sig,
    }
    assert verify_signature(SECRET, headers, b'{"type":"tampered"}') is False


def test_verify_signature_rejects_stale_timestamp():
    body = b'{"type":"message"}'
    msg_id = "msg_03"
    ts = int(time.time()) - 3600  # one hour ago
    sig = _sign(msg_id, ts, body)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sig,
    }
    assert verify_signature(SECRET, headers, body) is False


def test_verify_signature_accepts_one_of_multiple_signatures():
    body = b'{"type":"message"}'
    msg_id = "msg_04"
    ts = int(time.time())
    good = _sign(msg_id, ts, body)
    bad = "v1," + base64.b64encode(b"wrong-bytes-of-correct-length-32!").decode("ascii")
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": f"{bad} {good}",
    }
    assert verify_signature(SECRET, headers, body) is True


def test_verify_signature_rejects_missing_headers():
    assert verify_signature(SECRET, {}, b'{"x":1}') is False


def test_verify_signature_accepts_raw_secret_without_prefix():
    body = b'{"type":"message"}'
    msg_id = "msg_05"
    ts = int(time.time())
    sig = _sign(msg_id, ts, body)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sig,
    }
    raw_secret = base64.b64encode(SECRET_BYTES).decode("ascii")
    assert verify_signature(raw_secret, headers, body) is True


class _WebhookRequest:
    def __init__(self, body: bytes, headers: Dict[str, str]):
        self._body = body
        self.headers = headers

    async def read(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_webhook_handler_echoes_valid_signed_verification():
    import json

    adapter, _ = _make_adapter()
    body = json.dumps({
        "type": "webhook.verification",
        "eventId": "evt-verify",
        "timestamp": "2026-07-20T12:00:00Z",
        "apiVersion": "2026-07-07",
        "data": {"challenge": "challenge-token", "event": "chat.message"},
    }).encode("utf-8")
    msg_id = "msg_verify"
    ts = int(time.time())
    response = await adapter._handle_webhook(_WebhookRequest(body, {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": _sign(msg_id, ts, body),
    }))

    assert response.status == 200
    assert json.loads(response.body) == {"challenge": "challenge-token"}
    assert len(adapter._dedup_webhook_id._seen) == 0


@pytest.mark.asyncio
async def test_webhook_handler_rejects_invalid_verification_signature():
    body = b'{"type":"webhook.verification","data":{"challenge":"token"}}'
    response = await _make_adapter()[0]._handle_webhook(_WebhookRequest(body, {
        "webhook-id": "msg_verify",
        "webhook-timestamp": str(int(time.time())),
        "webhook-signature": "v1,invalid",
    }))

    assert response.status == 401


# ---------------------------------------------------------------------------
# Mention parsing
# ---------------------------------------------------------------------------

BOT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def test_strip_bot_mention_removes_only_bot():
    text = f"<@{BOT_ID}> hello <@{OTHER_ID}>"
    assert _strip_bot_mention(text, BOT_ID) == f"hello <@{OTHER_ID}>"


def test_strip_bot_mention_handles_bang_form():
    text = f"<!@{BOT_ID}> ping"
    assert _strip_bot_mention(text, BOT_ID) == "ping"


def test_was_bot_mentioned_true_when_matches():
    assert _was_bot_mentioned(f"hi <@{BOT_ID}>", BOT_ID) is True


def test_was_bot_mentioned_false_when_other_user():
    assert _was_bot_mentioned(f"hi <@{OTHER_ID}>", BOT_ID) is False


def test_was_bot_mentioned_false_without_bot_id():
    assert _was_bot_mentioned(f"hi <@{BOT_ID}>", None) is False


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def test_deduplicator_blocks_repeat():
    d = _Deduplicator(max_size=100)
    assert d.is_duplicate("a") is False
    assert d.is_duplicate("a") is True
    assert d.is_duplicate("b") is False


def test_deduplicator_evicts_when_full():
    d = _Deduplicator(max_size=10)
    for i in range(20):
        d.is_duplicate(f"k{i}")
    # Cache should have evicted at least once.
    assert len(d._seen) < 20


# ---------------------------------------------------------------------------
# thread_id metadata helpers
# ---------------------------------------------------------------------------

def test_thread_ts_from_metadata_reads_thread_id_string():
    assert _thread_ts_from_metadata({"thread_id": "1700000000000000"}) == 1700000000000000


def test_thread_ts_from_metadata_reads_thread_timestamp_int():
    assert _thread_ts_from_metadata({"thread_timestamp": 42}) == 42


def test_thread_ts_from_metadata_returns_none_on_empty():
    assert _thread_ts_from_metadata(None) is None
    assert _thread_ts_from_metadata({}) is None
    assert _thread_ts_from_metadata({"thread_id": ""}) is None
    assert _thread_ts_from_metadata({"thread_id": "not-a-number"}) is None


# ---------------------------------------------------------------------------
# reply_to metadata helper
# ---------------------------------------------------------------------------

def test_reply_ts_from_metadata_reads_explicit_keys():
    assert _reply_ts_from_metadata({"reply_to_timestamp": 100}) == 100
    assert _reply_ts_from_metadata({"replyToTimestamp": "200"}) == 200
    assert _reply_ts_from_metadata({"replyTimestamp": 300}) == 300


def test_reply_ts_from_metadata_returns_none_when_absent():
    assert _reply_ts_from_metadata(None) is None
    assert _reply_ts_from_metadata({}) is None
    assert _reply_ts_from_metadata({"unrelated": 5}) is None


# ---------------------------------------------------------------------------
# expand_soft_breaks
# ---------------------------------------------------------------------------

def test_expand_soft_breaks_inserts_blank_between_lines():
    assert expand_soft_breaks("a\nb\nc") == "a\n\nb\n\nc"


def test_expand_soft_breaks_preserves_existing_paragraphs():
    assert expand_soft_breaks("a\n\nb") == "a\n\nb"


def test_expand_soft_breaks_preserves_fenced_code_block():
    src = "Run this:\n```sh\nls -la\necho hi\n```\nDone."
    out = expand_soft_breaks(src)
    # Lines inside the fence are unmodified.
    assert "```sh\nls -la\necho hi\n```" in out
    # The non-code line before the fence gets paragraph-separated.
    assert out.startswith("Run this:\n\n```sh")


def test_expand_soft_breaks_handles_tilde_fences():
    src = "before\n~~~\ncode line\n~~~\nafter"
    out = expand_soft_breaks(src)
    assert "~~~\ncode line\n~~~" in out


def test_expand_soft_breaks_is_idempotent_on_empty():
    assert expand_soft_breaks("") == ""
    assert expand_soft_breaks("single line") == "single line"


def test_expand_soft_breaks_deindents_fence_inside_list_item():
    """The agent emits fences indented to match list-item content. Roam's
    renderer mis-parses indented fences as inline code, so we de-indent
    them to column 0.
    """
    src = "1. Run this:\n   ```bash\n   hermes setup\n   ```\n   Continue."
    out = expand_soft_breaks(src)
    # Fence + content + close are all at column 0
    assert "\n```bash\nhermes setup\n```\n" in out


def test_expand_soft_breaks_handles_four_space_indented_fence():
    """CommonMark requires fences at 0–3 spaces of indent, but the agent
    sometimes emits 4-space indented fences (a common Markdown style).
    We catch them too and de-indent.
    """
    src = "- Item:\n    ```\n    code\n    ```"
    out = expand_soft_breaks(src)
    assert "\n```\ncode\n```" in out


def test_expand_soft_breaks_preserves_relative_indent_inside_fence():
    """Stripping the fence's leading whitespace from content lines must
    only remove up to fence_indent spaces — extra indentation inside the
    code (e.g., a Python function body) is preserved.
    """
    src = "   ```py\n   def f():\n       return 1\n   ```"
    out = expand_soft_breaks(src)
    # Fence + first content line de-indented by 3; the inner 4-space indent
    # on `return` becomes 4 (was 7), preserving the relative offset.
    assert "```py\ndef f():\n    return 1\n```" in out


def test_expand_soft_breaks_unindented_fence_is_unchanged():
    src = "before\n```sh\nrun\n```\nafter"
    out = expand_soft_breaks(src)
    assert "```sh\nrun\n```" in out


# ---------------------------------------------------------------------------
# Adapter fixture + fake RoamClient
# ---------------------------------------------------------------------------

class _FakeRoamClient:
    """In-memory replacement for RoamClient used in adapter unit tests."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []
        self.typings: List[Dict[str, Any]] = []
        self.subscribes: List[Tuple[str, str]] = []
        self.unsubscribes: List[str] = []
        self.next_webhook_id: str = "wh-test-id"
        self.next_timestamp: int = 1700000000000000
        self.token_info_response: Dict[str, Any] = {
            "bot": {"id": BOT_ID, "name": "TestBot"}
        }
        self.fail_post_with: Optional[RoamAPIError] = None

    async def chat_post(self, chat_id, text, **kwargs):
        if self.fail_post_with is not None:
            raise self.fail_post_with
        ts = self.next_timestamp
        self.next_timestamp += 1
        record = {"chat_id": chat_id, "text": text, **kwargs}
        self.posts.append(record)
        return {
            "chatId": chat_id,
            "timestamp": ts,
            "threadTimestamp": kwargs.get("thread_timestamp", 0),
        }

    async def chat_update(self, chat_id, timestamp, text, **kwargs):
        self.updates.append({
            "chat_id": chat_id,
            "timestamp": timestamp,
            "text": text,
            **kwargs,
        })
        return {"chatId": chat_id, "timestamp": timestamp}

    async def chat_typing(self, chat_id, **kwargs):
        self.typings.append({"chat_id": chat_id, **kwargs})
        return {}

    async def webhook_subscribe(self, url, event="chat.message"):
        self.subscribes.append((url, event))
        return {"id": self.next_webhook_id, "url": url, "event": event}

    async def webhook_unsubscribe(self, webhook_id):
        self.unsubscribes.append(webhook_id)
        return None

    async def token_info(self):
        return self.token_info_response


def _make_adapter(
    *,
    allow_all: bool = False,
    allowed_users: Optional[List[str]] = None,
    allowed_groups: Optional[List[str]] = None,
    require_mention: bool = True,
    reply_in_thread: bool = False,
    bot_user_id: Optional[str] = BOT_ID,
    owner_id: Optional[str] = None,
    owner_name: Optional[str] = None,
    owner_email: Optional[str] = None,
) -> Tuple[RoamAdapter, _FakeRoamClient]:
    from gateway.config import PlatformConfig

    config = PlatformConfig(extra={
        "api_key": "test-api-key",
        "webhook_secret": SECRET,
        "allow_all_users": allow_all,
        "allowed_users": allowed_users or [],
        "allowed_groups": allowed_groups or [],
        "require_mention": require_mention,
        "reply_in_thread": reply_in_thread,
    })
    adapter = RoamAdapter(config)
    fake = _FakeRoamClient()
    adapter._client = fake
    adapter._bot_user_id = bot_user_id
    adapter._owner_id = owner_id
    adapter._owner_name = owner_name
    adapter._owner_email = owner_email
    return adapter, fake


# ---------------------------------------------------------------------------
# Outbound: send / edit / typing / handoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_returns_timestamp_as_message_id():
    adapter, fake = _make_adapter()
    result = await adapter.send("chat-1", "hello")
    assert result.success is True
    assert result.message_id == "1700000000000000"
    assert fake.posts[0]["text"] == "hello"
    assert fake.posts[0]["thread_timestamp"] is None


@pytest.mark.asyncio
async def test_send_routes_thread_id_from_metadata():
    adapter, fake = _make_adapter()
    await adapter.send("chat-1", "hi", metadata={"thread_id": "1700000000000000"})
    assert fake.posts[0]["thread_timestamp"] == 1700000000000000


@pytest.mark.asyncio
async def test_send_ignores_gateway_reply_to():
    """The gateway always passes the inbound message as reply_to. We drop it
    so Roam doesn't render a quote block on every turn-based response.
    """
    adapter, fake = _make_adapter()
    await adapter.send("chat-1", "hi", reply_to="1700000000000123")
    assert fake.posts[0]["reply_to"] is None


@pytest.mark.asyncio
async def test_send_honors_explicit_reply_to_metadata():
    """An explicit reply_to_timestamp in metadata DOES set replyTo —
    that's the escape hatch for referencing an older message.
    """
    adapter, fake = _make_adapter()
    await adapter.send(
        "chat-1",
        "hi",
        reply_to="1700000000000123",
        metadata={"reply_to_timestamp": 1699999999000000},
    )
    assert fake.posts[0]["reply_to"] == 1699999999000000


@pytest.mark.asyncio
async def test_send_expands_soft_breaks_in_outgoing_text():
    adapter, fake = _make_adapter()
    await adapter.send("chat-1", "line one\nline two\nline three")
    assert fake.posts[0]["text"] == "line one\n\nline two\n\nline three"


@pytest.mark.asyncio
async def test_edit_message_expands_soft_breaks():
    adapter, fake = _make_adapter()
    await adapter.edit_message("chat-1", "1700000000000000", "a\nb")
    assert fake.updates[0]["text"] == "a\n\nb"


@pytest.mark.asyncio
async def test_send_surfaces_api_error():
    adapter, fake = _make_adapter()
    fake.fail_post_with = RoamAPIError(413, "Message too long")
    result = await adapter.send("chat-1", "x" * 10)
    assert result.success is False
    assert "413" in result.error
    assert result.retryable is False


@pytest.mark.asyncio
async def test_send_token_revoked_is_terminal_and_fatal():
    """token_revoked must not be retried — flag a fatal non-retryable error."""
    adapter, fake = _make_adapter()
    fake.fail_post_with = RoamAPIError(
        401, '{"ok":false,"error":"token_revoked"}'
    )
    result = await adapter.send("chat-1", "hello")
    assert result.success is False
    assert result.retryable is False
    assert "token_revoked" in (result.error or "")
    assert adapter._fatal_error is not None
    assert adapter._fatal_error["code"] == "token_revoked"
    assert adapter._fatal_error["retryable"] is False


@pytest.mark.asyncio
async def test_send_5xx_is_retryable_without_fatal():
    adapter, fake = _make_adapter()
    fake.fail_post_with = RoamAPIError(503, "temporary")
    result = await adapter.send("chat-1", "hello")
    assert result.success is False
    assert result.retryable is True
    assert adapter._fatal_error is None


@pytest.mark.asyncio
async def test_edit_message_calls_chat_update():
    adapter, fake = _make_adapter()
    result = await adapter.edit_message("chat-1", "1700000000000000", "edited")
    assert result.success is True
    assert fake.updates[0] == {
        "chat_id": "chat-1",
        "timestamp": 1700000000000000,
        "text": "edited",
    }


@pytest.mark.asyncio
async def test_edit_message_rejects_non_numeric_message_id():
    adapter, _ = _make_adapter()
    result = await adapter.edit_message("chat-1", "not-a-number", "x")
    assert result.success is False
    assert "int timestamp" in result.error


@pytest.mark.asyncio
async def test_send_typing_passes_thread_timestamp():
    adapter, fake = _make_adapter()
    await adapter.send_typing("chat-1", metadata={"thread_id": "42"})
    assert fake.typings[0] == {"chat_id": "chat-1", "thread_timestamp": 42}


@pytest.mark.asyncio
async def test_create_handoff_thread_returns_timestamp_string():
    adapter, fake = _make_adapter()
    tid = await adapter.create_handoff_thread("chat-1", "New task")
    assert tid == str(1700000000000000)
    assert fake.posts[0]["text"] == "New task"
    # The seed post itself has no threadTimestamp — it anchors a new thread.
    assert "thread_timestamp" not in fake.posts[0] or fake.posts[0]["thread_timestamp"] is None


# ---------------------------------------------------------------------------
# Inbound: dispatch_event
# ---------------------------------------------------------------------------

def _msg_event(**overrides) -> Dict[str, Any]:
    base = {
        "type": "message",
        "version": 1,
        "contentType": "text",
        "userId": OTHER_ID,
        "chatId": "chat-1",
        "chatType": "dm",
        "text": "hello",
        "timestamp": 1700000000000000,
        "messageId": "msg-1",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_inbound_dm_allowed_user_is_handled():
    adapter, _ = _make_adapter(allowed_users=[OTHER_ID])
    await adapter._dispatch_event(_msg_event())
    assert len(adapter.handled_messages) == 1
    event = adapter.handled_messages[0]
    assert event.text == "hello"
    assert event.source.chat_type == "dm"
    assert event.source.user_id == OTHER_ID
    assert event.source.thread_id is None


@pytest.mark.asyncio
async def test_inbound_dm_unknown_user_is_rejected():
    adapter, _ = _make_adapter(allowed_users=[])
    await adapter._dispatch_event(_msg_event())
    assert adapter.handled_messages == []


@pytest.mark.asyncio
async def test_inbound_self_echo_is_filtered():
    adapter, _ = _make_adapter(allowed_users=[OTHER_ID])
    await adapter._dispatch_event(_msg_event(userId=BOT_ID))
    assert adapter.handled_messages == []


@pytest.mark.asyncio
async def test_inbound_group_without_mention_is_silenced(caplog):
    import logging
    adapter, _ = _make_adapter(allowed_groups=["chat-1"], require_mention=True)
    with caplog.at_level(logging.INFO, logger="roam.adapter"):
        await adapter._dispatch_event(
            _msg_event(chatType="group", text="general chatter, no ping")
        )
    assert adapter.handled_messages == []
    # Drop should now be visible in logs so users can debug
    # "why didn't the bot respond?" without enabling DEBUG.
    assert any("require_mention=true" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_inbound_group_with_mention_strips_and_handles():
    adapter, _ = _make_adapter(allowed_groups=["chat-1"], require_mention=True)
    await adapter._dispatch_event(
        _msg_event(
            chatType="group",
            text=f"<@{BOT_ID}> hello there",
        )
    )
    assert len(adapter.handled_messages) == 1
    assert adapter.handled_messages[0].text == "hello there"
    assert adapter.handled_messages[0].source.chat_type == "group"


@pytest.mark.asyncio
async def test_inbound_threads_roundtrip_thread_id():
    adapter, _ = _make_adapter(allowed_users=[OTHER_ID])
    await adapter._dispatch_event(_msg_event(threadTimestamp=1699999999000000))
    event = adapter.handled_messages[0]
    assert event.source.thread_id == "1699999999000000"


@pytest.mark.asyncio
async def test_reply_in_thread_anchors_on_inbound_when_top_level():
    """With reply_in_thread on, a top-level inbound in a GROUP gets thread_id
    set to its own timestamp so the bot's reply creates a thread off the post.
    (DMs are excluded — see test_reply_in_thread_skips_dm_top_level.)
    """
    adapter, _ = _make_adapter(
        allow_all=True, reply_in_thread=True, require_mention=False
    )
    await adapter._dispatch_event(
        _msg_event(timestamp=1700000000000111, chatType="group")
    )
    event = adapter.handled_messages[0]
    assert event.source.thread_id == "1700000000000111"


@pytest.mark.asyncio
async def test_reply_in_thread_skips_dm_top_level():
    """reply_in_thread anchors only group posts, never DMs: a top-level DM
    stays top-level (thread_id None) even with reply_in_thread on. DMs are
    already a private 1:1 timeline, so anchoring every message on itself would
    fragment the conversation into one thread per message.
    """
    adapter, _ = _make_adapter(allow_all=True, reply_in_thread=True)
    await adapter._dispatch_event(_msg_event(timestamp=1700000000000111))
    event = adapter.handled_messages[0]
    assert event.source.chat_type == "dm"
    assert event.source.thread_id is None


@pytest.mark.asyncio
async def test_reply_in_thread_preserves_existing_thread():
    """With reply_in_thread on, an already-threaded inbound keeps its
    existing thread_id (the bot replies in the existing thread, not a
    new one anchored on itself).
    """
    adapter, _ = _make_adapter(allow_all=True, reply_in_thread=True)
    await adapter._dispatch_event(
        _msg_event(timestamp=1700000000000111, threadTimestamp=1699999999000000)
    )
    event = adapter.handled_messages[0]
    assert event.source.thread_id == "1699999999000000"


@pytest.mark.asyncio
async def test_reply_in_thread_off_keeps_top_level_top_level():
    adapter, _ = _make_adapter(allow_all=True, reply_in_thread=False)
    await adapter._dispatch_event(_msg_event(timestamp=1700000000000111))
    event = adapter.handled_messages[0]
    assert event.source.thread_id is None


@pytest.mark.asyncio
async def test_inbound_dedup_drops_repeat():
    adapter, _ = _make_adapter(allowed_users=[OTHER_ID])
    event = _msg_event()
    await adapter._dispatch_event(event)
    await adapter._dispatch_event(event)
    assert len(adapter.handled_messages) == 1


@pytest.mark.asyncio
async def test_inbound_allow_all_short_circuits_allowlist():
    adapter, _ = _make_adapter(allow_all=True, allowed_users=[])
    await adapter._dispatch_event(_msg_event())
    assert len(adapter.handled_messages) == 1


@pytest.mark.asyncio
async def test_inbound_drops_non_message_events():
    adapter, _ = _make_adapter(allow_all=True)
    await adapter._dispatch_event({"type": "chat.reaction", "chatId": "x"})
    assert adapter.handled_messages == []


def test_unwrap_webhook_envelope_bare_passthrough():
    bare = _msg_event()
    assert unwrap_webhook_envelope(bare) is bare or unwrap_webhook_envelope(bare) == bare
    assert unwrap_webhook_envelope(bare)["type"] == "message"


def test_unwrap_webhook_envelope_2026_07_07():
    """apiVersion + data marks the common event envelope; restore type: message."""
    inner = _msg_event(chatId="chat-env", text="from envelope")
    del inner["type"]  # envelope data may omit the legacy discriminator
    enveloped = {
        "type": "chat.message",
        "eventId": "0197f9a1-7d2e-7cc3-9f6a-8b1c2d3e4f5a",
        "timestamp": "2026-07-07T18:23:45.123456Z",
        "apiVersion": "2026-07-07",
        "data": inner,
    }
    out = unwrap_webhook_envelope(enveloped)
    assert out["type"] == "message"
    assert out["chatId"] == "chat-env"
    assert out["text"] == "from envelope"


@pytest.mark.asyncio
async def test_inbound_handles_enveloped_chat_message():
    adapter, _ = _make_adapter(allow_all=True)
    inner = _msg_event(text="envelope hello")
    del inner["type"]
    await adapter._dispatch_event(
        {
            "type": "chat.message",
            "eventId": "evt-1",
            "timestamp": "2026-07-07T18:00:00Z",
            "apiVersion": "2026-07-07",
            "data": inner,
        }
    )
    assert len(adapter.handled_messages) == 1
    assert adapter.handled_messages[0].text == "envelope hello"


@pytest.mark.asyncio
async def test_inbound_ignores_enveloped_edits():
    adapter, _ = _make_adapter(allow_all=True)
    await adapter._dispatch_event(
        {
            "type": "chat.message",
            "eventId": "evt-2",
            "timestamp": "2026-07-07T18:00:00Z",
            "apiVersion": "2026-07-07",
            "data": _msg_event(version=2, text="edited"),
        }
    )
    assert adapter.handled_messages == []


@pytest.mark.asyncio
async def test_inbound_ignores_edited_message(caplog):
    """v1 chat.message fires for edits (version > 1). Do not re-invoke the agent."""
    import logging

    adapter, _ = _make_adapter(allow_all=True)
    with caplog.at_level(logging.DEBUG, logger="roam.adapter"):
        await adapter._dispatch_event(
            _msg_event(version=2, text="edited content")
        )
    assert adapter.handled_messages == []
    assert any("ignoring edited message" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_inbound_ignores_deleted_message(caplog):
    """v1 chat.message fires for deletes (contentType=deleted). Ignore them."""
    import logging

    adapter, _ = _make_adapter(allow_all=True)
    with caplog.at_level(logging.DEBUG, logger="roam.adapter"):
        await adapter._dispatch_event(
            _msg_event(version=3, contentType="deleted", text="")
        )
    assert adapter.handled_messages == []
    assert any("ignoring deleted message" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_inbound_accepts_version_1_create():
    adapter, _ = _make_adapter(allow_all=True)
    await adapter._dispatch_event(_msg_event(version=1))
    assert len(adapter.handled_messages) == 1


@pytest.mark.asyncio
async def test_inbound_accepts_missing_version_as_create():
    """Older/test payloads without version still process as creates."""
    adapter, _ = _make_adapter(allow_all=True)
    event = _msg_event()
    del event["version"]
    await adapter._dispatch_event(event)
    assert len(adapter.handled_messages) == 1


# ---------------------------------------------------------------------------
# Inbound media (attachment url population)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_media_downloads_when_url_present(monkeypatch):
    """When the webhook item carries a signed url (the common case after the
    appserver's ingest poll), fetch and cache the bytes for the vision path.
    """
    adapter, _ = _make_adapter()

    class _Resp:
        status = 200

        async def read(self):
            return b"fake-image-bytes"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, headers=None):
            assert url == "https://assets-cdn.example/content?sig=1"
            return _Resp()

    monkeypatch.setattr("aiohttp.ClientSession", _Session)

    media_urls: List[str] = []
    media_types: List[str] = []
    await adapter._collect_media(
        [{
            "type": "photo",
            "mime": "image/png",
            "name": "image.png",
            "url": "https://assets-cdn.example/content?sig=1",
            "assetId": "asset-1",
        }],
        media_urls,
        media_types,
    )
    assert len(media_urls) == 1
    assert media_types == ["image/png"]
    # cache_image_from_bytes wrote a real tempfile; clean up.
    from pathlib import Path
    Path(media_urls[0]).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_collect_media_skips_items_without_url():
    """Metadata-only items (url still empty after ingest poll) are skipped."""
    adapter, _ = _make_adapter()
    media_urls: List[str] = []
    media_types: List[str] = []
    await adapter._collect_media(
        [{
            "type": "photo",
            "mime": "image/png",
            "name": "image.png",
            "assetId": "asset-still-ingesting",
        }],
        media_urls,
        media_types,
    )
    assert media_urls == []
    assert media_types == []


@pytest.mark.asyncio
async def test_inbound_with_media_url_sets_photo_type(monkeypatch):
    adapter, _ = _make_adapter(allow_all=True)

    async def _fake_collect(items, media_urls, media_types):
        media_urls.append("/tmp/cached.png")
        media_types.append("image/png")

    adapter._collect_media = _fake_collect  # type: ignore[method-assign]
    await adapter._dispatch_event(
        _msg_event(
            text="",
            items=[{
                "type": "photo",
                "mime": "image/png",
                "url": "https://assets-cdn.example/x",
                "assetId": "a1",
            }],
        )
    )
    assert len(adapter.handled_messages) == 1
    event = adapter.handled_messages[0]
    assert event.media_urls == ["/tmp/cached.png"]
    assert event.media_types == ["image/png"]
    from gateway.platforms.base import MessageType
    assert event.message_type == MessageType.PHOTO


# ---------------------------------------------------------------------------
# token.info → owner identity
# ---------------------------------------------------------------------------

def test_apply_token_info_pat_extracts_owner_and_bot():
    adapter, _ = _make_adapter(bot_user_id=None)
    adapter._apply_token_info({
        "user": {"id": "owner-uuid", "name": "Rob", "email": "rob@ro.am"},
        "bot": {"id": BOT_ID, "name": "Hermes"},
        "scopes": ["chat:write", "user:read.email"],
    })
    assert adapter._bot_user_id == BOT_ID
    assert adapter._bot_name == "Hermes"
    assert adapter._owner_id == "owner-uuid"
    assert adapter._owner_name == "Rob"
    assert adapter._owner_email == "rob@ro.am"


def test_apply_token_info_org_token_has_no_owner():
    adapter, _ = _make_adapter(bot_user_id=None)
    adapter._apply_token_info({
        "user": {"id": BOT_ID, "name": "OrgBot"},
        "scopes": ["chat:write"],
    })
    assert adapter._bot_user_id == BOT_ID
    assert adapter._bot_name == "OrgBot"
    assert adapter._owner_id is None
    assert adapter._owner_name is None
    assert adapter._owner_email is None


def test_apply_token_info_pat_without_email_scope():
    adapter, _ = _make_adapter(bot_user_id=None)
    adapter._apply_token_info({
        "user": {"id": "owner-uuid", "name": "Rob"},
        "bot": {"id": BOT_ID, "name": "Hermes"},
    })
    assert adapter._owner_name == "Rob"
    assert adapter._owner_email is None


@pytest.mark.asyncio
async def test_inbound_from_owner_uses_owner_name_and_email_context():
    adapter, _ = _make_adapter(
        allow_all=True,
        owner_id="owner-uuid",
        owner_name="Rob",
        owner_email="rob@ro.am",
    )
    await adapter._dispatch_event(_msg_event(userId="owner-uuid"))
    event = adapter.handled_messages[0]
    assert event.source.user_name == "Rob"
    assert event.source.user_id == "owner-uuid"
    assert event.channel_context == "Owner email: rob@ro.am"


@pytest.mark.asyncio
async def test_inbound_from_non_owner_falls_back_to_uuid():
    adapter, _ = _make_adapter(
        allow_all=True,
        owner_id="owner-uuid",
        owner_name="Rob",
        owner_email="rob@ro.am",
    )
    await adapter._dispatch_event(_msg_event(userId="stranger-uuid"))
    event = adapter.handled_messages[0]
    assert event.source.user_name == "stranger-uuid"
    # Don't leak owner email to other senders.
    assert event.channel_context is None


# ---------------------------------------------------------------------------
# Standalone send (cron path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standalone_send_uses_ephemeral_client(monkeypatch):
    from roam import adapter as adapter_mod

    fake = _FakeRoamClient()

    def _fake_client(*args, **kwargs):
        return fake

    monkeypatch.setattr(adapter_mod, "RoamClient", _fake_client)
    monkeypatch.setenv("ROAM_API_KEY", "test-key")

    class _PConfig:
        extra = {}

    result = await adapter_mod._standalone_send(
        _PConfig(),
        "chat-1",
        "hello from cron",
    )
    assert result == {"success": True, "message_id": "1700000000000000"}
    assert fake.posts[0]["text"] == "hello from cron"


@pytest.mark.asyncio
async def test_standalone_send_appends_media_hint(monkeypatch):
    from roam import adapter as adapter_mod

    fake = _FakeRoamClient()
    monkeypatch.setattr(adapter_mod, "RoamClient", lambda *a, **k: fake)
    monkeypatch.setenv("ROAM_API_KEY", "test-key")

    class _PConfig:
        extra = {}

    await adapter_mod._standalone_send(
        _PConfig(), "chat-1", "see attached", media_files=["a.png", "b.png"]
    )
    assert "2 attachment" in fake.posts[0]["text"]


# ---------------------------------------------------------------------------
# Webhook subscription lifecycle (subscribe id capture + unsubscribe by id)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_unsubscribes_by_id():
    """disconnect() deletes the subscription using the id captured at
    subscribe time. webhook.unsubscribe keys on the server-assigned id, not
    on url+event, so the adapter must round-trip the id — not the URL."""
    adapter, fake = _make_adapter()
    adapter._subscribed_webhook_id = "wh-123"
    adapter._subscribed_url = "https://example.com/roam/webhook"

    await adapter.disconnect()

    assert fake.unsubscribes == ["wh-123"]
    assert adapter._subscribed_webhook_id is None
    assert adapter._subscribed_url is None


@pytest.mark.asyncio
async def test_disconnect_without_subscription_id_is_noop():
    """No retained id (subscribe failed, or no public_url) → no unsubscribe
    call rather than a doomed url-based one."""
    adapter, fake = _make_adapter()

    await adapter.disconnect()

    assert fake.unsubscribes == []
