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
        self.unsubscribes: List[Tuple[str, str]] = []
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
        return {}

    async def webhook_unsubscribe(self, url, event="chat.message"):
        self.unsubscribes.append((url, event))
        return {}

    async def token_info(self):
        return self.token_info_response


def _make_adapter(
    *,
    allow_all: bool = False,
    allowed_users: Optional[List[str]] = None,
    allowed_groups: Optional[List[str]] = None,
    require_mention: bool = True,
    bot_user_id: Optional[str] = BOT_ID,
) -> Tuple[RoamAdapter, _FakeRoamClient]:
    from gateway.config import PlatformConfig

    config = PlatformConfig(extra={
        "api_key": "test-api-key",
        "webhook_secret": SECRET,
        "allow_all_users": allow_all,
        "allowed_users": allowed_users or [],
        "allowed_groups": allowed_groups or [],
        "require_mention": require_mention,
    })
    adapter = RoamAdapter(config)
    fake = _FakeRoamClient()
    adapter._client = fake
    adapter._bot_user_id = bot_user_id
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
async def test_inbound_group_without_mention_is_silenced():
    adapter, _ = _make_adapter(allowed_groups=["chat-1"], require_mention=True)
    await adapter._dispatch_event(
        _msg_event(chatType="group", text="general chatter, no ping")
    )
    assert adapter.handled_messages == []


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
