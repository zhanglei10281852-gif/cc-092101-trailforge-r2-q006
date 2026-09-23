from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from trailforge.errors import ActionPackIntegrityError
from trailforge.schemas.actionpack import PACK_FORMAT, PACK_VERSION

DIGEST_KEY = "digest"
SIGNATURE_KEY = "signature"
MANIFEST_KEY = "manifest"
SIGNATURE_ALGORITHM = "HMAC-SHA256"

# Envelope fields that never participate in the canonical digest.
_ENVELOPE_META_KEYS = {MANIFEST_KEY, DIGEST_KEY, SIGNATURE_KEY}


def canonical_json(document: dict[str, Any]) -> str:
    """Deterministic UTF-8 JSON used for digest and signature verification."""
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_digest(document: dict[str, Any]) -> str:
    payload = canonical_json(document).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_signature(document: dict[str, Any], secret: str) -> str:
    payload = canonical_json(document).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def seal_pack(manifest: dict[str, Any], *, secret: str | None = None) -> dict[str, Any]:
    envelope: dict[str, Any] = {MANIFEST_KEY: manifest, DIGEST_KEY: ""}
    digest = compute_digest(manifest)
    envelope[DIGEST_KEY] = digest
    if secret:
        envelope[SIGNATURE_KEY] = compute_signature(manifest, secret)
    return envelope


def open_pack(
    envelope: Any,
    *,
    secret: str | None = None,
) -> dict[str, Any]:
    """Verify envelope digest/signature and return the manifest.

    Raises ActionPackIntegrityError on any structural or cryptographic problem.
    This performs no database access and must complete before any write.
    """
    if not isinstance(envelope, dict):
        raise ActionPackIntegrityError("action pack must be a JSON object")
    manifest = envelope.get(MANIFEST_KEY)
    digest = envelope.get(DIGEST_KEY)
    if not isinstance(manifest, dict):
        raise ActionPackIntegrityError("action pack is missing its manifest")
    if not isinstance(digest, str) or not digest:
        raise ActionPackIntegrityError("action pack is missing its digest")
    expected = compute_digest(manifest)
    if not hmac.compare_digest(expected, digest):
        raise ActionPackIntegrityError(
            "action pack digest mismatch; file is corrupted or tampered with",
            context={"expected": expected, "provided": digest},
        )
    signature = envelope.get(SIGNATURE_KEY)
    if signature is not None:
        if not secret:
            raise ActionPackIntegrityError(
                "action pack carries a signature but no verification secret is configured"
            )
        if not isinstance(signature, str):
            raise ActionPackIntegrityError("action pack signature must be a hex string")
        expected_sig = compute_signature(manifest, secret)
        if not hmac.compare_digest(expected_sig, signature):
            raise ActionPackIntegrityError("action pack signature verification failed")
    if manifest.get("format") != PACK_FORMAT:
        raise ActionPackIntegrityError(
            "unsupported action pack format", context={"format": manifest.get("format")}
        )
    if manifest.get("version") != PACK_VERSION:
        raise ActionPackIntegrityError(
            "unsupported action pack version", context={"version": manifest.get("version")}
        )
    return manifest


def encode_pack_bytes(envelope: dict[str, Any]) -> bytes:
    text = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return f"{text}\n".encode()


def decode_pack_bytes(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ActionPackIntegrityError("action pack is not valid UTF-8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionPackIntegrityError(
            "action pack is not valid JSON", context={"error": str(exc)}
        ) from exc
