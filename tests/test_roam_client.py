"""RoamClient request headers, version sourcing, and the API-version pin."""
from __future__ import annotations

from pathlib import Path

import roam_client


def _plugin_yaml_version() -> str:
    manifest = Path(roam_client.__file__).resolve().parent / "plugin.yaml"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError("no top-level version: in plugin.yaml")


def test_plugin_version_read_from_manifest():
    # Single source of truth: the runtime version is read from plugin.yaml,
    # not hardcoded — and the manifest must actually be found.
    assert roam_client.PLUGIN_VERSION == _plugin_yaml_version()
    assert roam_client.PLUGIN_VERSION != "0+unknown"


def test_headers_advertise_user_agent_and_pin_api_version():
    client = roam_client.RoamClient("test-key")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == f"hermes-roam/{roam_client.PLUGIN_VERSION}"
    # Pin on the envelope revision; handler still accepts bare baseline bodies.
    assert headers["Roam-Version"] == "2026-07-07"
    assert roam_client.ROAM_API_VERSION == "2026-07-07"


async def test_webhook_subscribe_includes_pinned_version():
    client = roam_client.RoamClient("test-key")
    captured: dict = {}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {}

    client._post = fake_post  # type: ignore[method-assign]
    await client.webhook_subscribe("https://example.test/webhook")

    assert captured["path"] == "/webhook.subscribe"
    assert captured["body"]["url"] == "https://example.test/webhook"
    assert captured["body"]["event"] == "chat.message"
    assert captured["body"]["version"] == "2026-07-07"


# ---------------------------------------------------------------------------
# Error body parsing / RoamAPIError machine codes
# ---------------------------------------------------------------------------

def test_parse_api_error_code_v1_shape():
    # v1: {"ok":false,"error":"<code>"} — machine code lives in ``error``.
    assert roam_client.parse_api_error_code(
        '{"ok":false,"error":"token_revoked"}'
    ) == "token_revoked"
    assert roam_client.parse_api_error_code(
        '{"ok":false,"error":"msg_too_long"}'
    ) == "msg_too_long"


def test_parse_api_error_code_v0_shape():
    # v0: human sentence in error, additive ``code`` field.
    assert roam_client.parse_api_error_code(
        '{"error":"Token has been revoked","code":"token_revoked"}'
    ) == "token_revoked"


def test_parse_api_error_code_ignores_human_messages_and_garbage():
    assert roam_client.parse_api_error_code(
        '{"error":"Message too long"}'
    ) is None
    assert roam_client.parse_api_error_code("not-json") is None
    assert roam_client.parse_api_error_code("") is None
    assert roam_client.parse_api_error_code("[]") is None


def test_roam_api_error_exposes_code_and_terminal():
    err = roam_client.RoamAPIError(
        401, '{"ok":false,"error":"token_revoked"}'
    )
    assert err.status == 401
    assert err.code == "token_revoked"
    assert err.terminal is True
    assert err.retryable is False
    assert "token_revoked" in str(err)


def test_roam_api_error_invalid_token_is_terminal():
    err = roam_client.RoamAPIError(
        401, '{"ok":false,"error":"invalid_token"}'
    )
    assert err.terminal is True
    assert err.retryable is False


def test_roam_api_error_5xx_is_retryable_without_code():
    err = roam_client.RoamAPIError(503, "upstream unavailable")
    assert err.code is None
    assert err.terminal is False
    assert err.retryable is True


def test_roam_api_error_4xx_catalog_not_retryable_by_default():
    err = roam_client.RoamAPIError(
        400, '{"ok":false,"error":"msg_too_long"}'
    )
    assert err.code == "msg_too_long"
    assert err.terminal is False
    assert err.retryable is False


def test_roam_api_error_ratelimited_is_retryable():
    err = roam_client.RoamAPIError(
        429, '{"ok":false,"error":"ratelimited"}'
    )
    assert err.code == "ratelimited"
    assert err.retryable is True
    assert err.terminal is False
