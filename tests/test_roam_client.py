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
    assert headers["Roam-Version"] == "2026-06-01"


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
    assert captured["body"]["version"] == "2026-06-01"
