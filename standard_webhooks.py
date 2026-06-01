"""Standard-Webhooks signature verification.

Roam signs outgoing webhooks per the Standard Webhooks spec
(https://www.standardwebhooks.com). Each delivery carries three headers:

  webhook-id         — unique message ID (UUID); reuse to dedupe retries
  webhook-timestamp  — Unix seconds when the server signed the payload
  webhook-signature  — space-separated list of ``v1,<base64>`` signatures

The signature is HMAC-SHA256 over ``${msgId}.${msgTimestamp}.${rawBody}``,
keyed by the base64-decoded secret (with the optional ``whsec_`` prefix
stripped). Multiple signatures may appear (key rotation) — any valid
signature passes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional


WHSEC_PREFIX = "whsec_"
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300


def _decode_secret(secret: str) -> bytes:
    """Decode a Standard-Webhooks secret to raw key bytes."""
    payload = secret[len(WHSEC_PREFIX):] if secret.startswith(WHSEC_PREFIX) else secret
    try:
        return base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return b""


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup."""
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def verify_signature(
    secret: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    *,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    now_fn=time.time,
) -> bool:
    """Verify a Standard-Webhooks signature.

    Returns True if the signature is valid AND the timestamp is within
    tolerance of now. False on missing headers, malformed values, stale
    timestamps, or a signature mismatch.
    """
    if not secret or not raw_body:
        return False

    msg_id = _get_header(headers, "webhook-id")
    msg_ts = _get_header(headers, "webhook-timestamp")
    msg_sig = _get_header(headers, "webhook-signature")
    if not msg_id or not msg_ts or not msg_sig:
        return False

    try:
        ts_seconds = int(msg_ts)
    except (TypeError, ValueError):
        return False
    if abs(int(now_fn()) - ts_seconds) > tolerance_seconds:
        return False

    secret_bytes = _decode_secret(secret)
    if not secret_bytes:
        return False

    signed = f"{msg_id}.{msg_ts}.".encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    ).decode("ascii")

    for sig in msg_sig.split():
        parts = sig.split(",", 1)
        if len(parts) != 2:
            continue
        version, value = parts
        if version != "v1":
            continue
        if hmac.compare_digest(value, expected):
            return True
    return False
