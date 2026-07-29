"""Receipt signing: semantic receipts as verifiable certificates (§6).

The endgame is "certificate authority for what enterprise data means" — so a
receipt should be checkable by someone who does not trust the channel it
arrived on. Every receipt is Ed25519-signed over its canonical JSON payload
(sorted keys, no whitespace, signature block excluded), with the signing
public key carried in the signature block for offline verification.

Trust model, honestly stated: the embedded public key makes a receipt
*tamper-evident*, not *identity-proof* — a forger could sign with their own
key. Identity comes from key distribution: publish your store's public key
(`semantix pubkey`) and verifiers pin it (`verify-receipt --public-key`).
Phase 0 keeps the private key in the store's SQLite (single-tenant, local
file); production custody belongs in a KMS behind the same two functions.
The payload's own integrity chain is unchanged — definition versions are
content-addressed fingerprint hashes either way.

No `cryptography` installed → receipts issue unsigned with a note, never a
failed attest (degradation ladder, as everywhere).
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

ALG = "ed25519"


def canonical_payload(receipt_dict: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers: the receipt minus its signature
    block, canonically serialized. Any reordering or reformatting of the JSON
    on the wire is harmless; any change to the *content* breaks the signature."""
    payload = {k: v for k, v in receipt_dict.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ensure_keypair(store) -> tuple[str, bytes, str] | None:
    """(key_id, private_bytes, public_b64) for this store, creating one on
    first use. None when cryptography is unavailable."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None

    row = store.latest_signing_key()
    if row is not None:
        return row["key_id"], _unb64(row["private_key"]), row["public_key"]

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())
    pub_b64 = _b64(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    key_id = "sk_" + uuid.uuid4().hex[:12]
    store.record_signing_key(key_id, _b64(priv_raw), pub_b64)
    return key_id, priv_raw, pub_b64


def sign_receipt(store, receipt) -> None:
    """Attach a signature block to a Receipt (mutates it). Unsigned + note
    when no crypto backend exists."""
    keypair = ensure_keypair(store)
    if keypair is None:
        receipt.signature = {"alg": None,
                             "note": "unsigned — cryptography not installed"}
        return
    key_id, priv_raw, pub_b64 = keypair
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(priv_raw)
    sig = priv.sign(canonical_payload(receipt.as_dict()))
    receipt.signature = {
        "alg": ALG,
        "key_id": key_id,
        "public_key": pub_b64,
        "signature": _b64(sig),
    }


def verify_receipt_dict(receipt_dict: dict[str, Any],
                        public_key_b64: str | None = None) -> tuple[bool, str]:
    """Verify a receipt dict. With `public_key_b64` the caller pins the
    expected signer; without it the embedded key is used (tamper-evidence
    only — say so in the message)."""
    # A receipt arriving from elsewhere is arbitrary JSON, so every field is
    # untrusted in TYPE as well as value — verification must return a verdict,
    # never raise.
    if not isinstance(receipt_dict, dict):
        return False, "receipt is not a JSON object"
    sig_block = receipt_dict.get("signature")
    if not isinstance(sig_block, dict) or sig_block.get("alg") != ALG:
        return False, "receipt is unsigned or uses an unknown algorithm"
    if not isinstance(sig_block.get("signature"), str) or \
            not isinstance(sig_block.get("public_key"), str):
        return False, "signature block is malformed"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, "cannot verify: cryptography not installed"

    pinned = public_key_b64 is not None
    # An EMPTY pin is not "no pin". `verify-receipt r.json --public-key
    # "$SEMANTIX_PUBKEY"` with the variable unset is the ordinary CI shape;
    # falling back to the receipt's OWN embedded key there verified every
    # forged receipt and reported "signature valid against pinned public
    # key". Identity checking is the one thing that separates a receipt from
    # a self-signed assertion, so a caller who asked to pin and supplied
    # nothing must be refused, not quietly downgraded.
    if pinned and not public_key_b64.strip():
        return False, ("a pinned public key was requested but is empty — refusing to "
                       "fall back to the key embedded in the receipt")
    # Stripped before comparison: a pin with a trailing newline — the shape a
    # `pubkey > key.txt` round-trip produces — failed as "signer key does not
    # match", which reads as tampering when the pin is merely unstripped.
    if pinned:
        public_key_b64 = public_key_b64.strip()
    key_b64 = public_key_b64 or sig_block.get("public_key", "")
    if pinned and sig_block.get("public_key") not in (None, key_b64):
        return False, "signer key does not match the pinned public key"
    try:
        pub = Ed25519PublicKey.from_public_bytes(_unb64(key_b64))
        pub.verify(_unb64(sig_block.get("signature", "")),
                   canonical_payload(receipt_dict))
    except (InvalidSignature, ValueError):
        return False, "signature INVALID — receipt content does not match"
    basis = "pinned public key" if pinned else "embedded public key (tamper-evidence only)"
    return True, f"signature valid against {basis}"


def receipt_trust(receipt: dict) -> tuple[bool, list[str]]:
    """(trustworthy, warnings) for a SIGNATURE-VALID receipt.

    A valid signature is not a valid status: the receipt carries
    `verified_as_of` and a freshness block precisely so verification can
    decay. This lives here so every verification surface — the CLI and the
    agent-facing MCP tool — applies the same decay; the first version was
    CLI-only, so the surface agents actually use kept trusting stale
    certificates. Never raises: malformed fields are distrust with a reason,
    on exactly the arbitrary-JSON input class verification exists for.
    """
    warnings: list[str] = []
    freshness = receipt.get("freshness")
    if freshness is not None and not isinstance(freshness, dict):
        return False, [f"freshness block is malformed ({type(freshness).__name__}, "
                       "not an object) — cannot evaluate decay; re-attest"]
    fresh_status = (freshness or {}).get("status")
    if fresh_status in ("stale", "partial"):
        warnings.append(
            f"this receipt's own freshness block reports `{fresh_status}` — the "
            "estate has drifted since it was issued; re-attest before relying on it")
        return False, warnings
    if not isinstance(receipt.get("status"), str):
        return False, ["receipt carries no status field — it certifies nothing"]
    return True, warnings
