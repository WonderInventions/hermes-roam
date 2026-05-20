"""Test fixtures + lightweight stubs for the Hermes gateway modules.

The roam-platform plugin imports from ``gateway.platforms.base``,
``gateway.config``, and ``gateway.session``. The plugin lives in its own
repo (``hermes-roam/``), so we install minimal stand-ins for those
modules at import time. Each stub provides just enough surface for the
adapter under test to load and exercise its outbound / inbound paths.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional


# Make the repo root importable so ``import roam`` works in tests.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# gateway.config
# ---------------------------------------------------------------------------

@dataclass
class _Platform:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass
class _PlatformConfig:
    extra: Dict[str, Any] = field(default_factory=dict)


_gateway_config = _ensure_module("gateway.config")
_gateway_config.Platform = _Platform
_gateway_config.PlatformConfig = _PlatformConfig


# ---------------------------------------------------------------------------
# gateway.session
# ---------------------------------------------------------------------------

@dataclass
class _SessionSource:
    platform: Any
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None
    chat_topic: Optional[str] = None
    user_id_alt: Optional[str] = None
    chat_id_alt: Optional[str] = None
    is_bot: bool = False
    guild_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    message_id: Optional[str] = None


_gateway_session = _ensure_module("gateway.session")
_gateway_session.SessionSource = _SessionSource


def _build_session_key(*args, **kwargs) -> str:
    return "test-session-key"


_gateway_session.build_session_key = _build_session_key


# ---------------------------------------------------------------------------
# gateway.platforms.base
# ---------------------------------------------------------------------------

class _MessageType(Enum):
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"


@dataclass
class _MessageEvent:
    text: str
    message_type: _MessageType = _MessageType.TEXT
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)


@dataclass
class _SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    continuation_message_ids: tuple = ()


def _cache_image_from_bytes(data: bytes, *, ext: str = ".bin") -> str:
    """Test stub — write to a fresh tempfile and return its path."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fh:
        fh.write(data)
        return fh.name


class _BasePlatformAdapter:
    REQUIRES_EDIT_FINALIZE = False

    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._connected = False
        self._fatal_error: Optional[Dict[str, Any]] = None
        self.handled_messages: List[_MessageEvent] = []

    def _mark_connected(self) -> None:
        self._connected = True

    def _mark_disconnected(self) -> None:
        self._connected = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._fatal_error = {"code": code, "message": message, "retryable": retryable}

    def build_source(self, **kwargs) -> _SessionSource:
        kwargs.setdefault("chat_type", "dm")
        return _SessionSource(platform=self.platform, **kwargs)

    async def handle_message(self, event: _MessageEvent) -> None:
        self.handled_messages.append(event)

    async def _keep_typing(self, *args, **kwargs) -> None:
        await asyncio.sleep(0)

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        return None

    async def edit_message(self, *args, **kwargs) -> _SendResult:
        return _SendResult(success=False, error="Not supported")

    async def create_handoff_thread(self, parent_chat_id: str, name: str) -> Optional[str]:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}


_gateway_platforms = _ensure_module("gateway.platforms")
_gateway_platforms_base = _ensure_module("gateway.platforms.base")
_gateway_platforms_base.BasePlatformAdapter = _BasePlatformAdapter
_gateway_platforms_base.MessageEvent = _MessageEvent
_gateway_platforms_base.MessageType = _MessageType
_gateway_platforms_base.SendResult = _SendResult
_gateway_platforms_base.cache_image_from_bytes = _cache_image_from_bytes


# ---------------------------------------------------------------------------
# gateway.status (optional — adapter falls back gracefully when missing)
# ---------------------------------------------------------------------------

_gateway_status = _ensure_module("gateway.status")


def _acquire_scoped_lock(*args, **kwargs) -> bool:
    return True


def _release_scoped_lock(*args, **kwargs) -> None:
    return None


_gateway_status.acquire_scoped_lock = _acquire_scoped_lock
_gateway_status.release_scoped_lock = _release_scoped_lock
