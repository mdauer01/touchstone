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

from dataclasses import dataclass

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


@dataclass(frozen=True)
class ReceiptAssessment:
    """Two separate decisions about a signature-valid receipt.

    `historical`: the receipt is a valid attestation about the instant it
    describes. `current_use`: the definition may be relied on for the estate
    as it is NOW. They were one boolean, so a historical receipt that was
    valid for its instant was reported to agents as `current_use: true` and
    printed "valid for CURRENT use — the estate was re-checked and matches"
    when nothing had been re-checked. A receipt about a past instant is
    never current, whatever its freshness says.
    """

    describes: str                 # "current" or "as_of"
    instant: str | None            # the moment the statement is about
    historical: bool
    current_use: bool
    warnings: tuple[str, ...] = ()
    rechecked_at: str | None = None   # when the live estate check ran, if one did

    @property
    def valid_for_stated_use(self) -> bool:
        """Valid as the KIND of statement the receipt makes: a current receipt
        must be current-use valid; an as-of receipt must be historically valid."""
        return self.current_use if self.describes == "current" else self.historical

    def reliance(self) -> str:
        """One human line naming what this receipt may be used for."""
        if self.describes == "as_of":
            if not self.historical:
                return "NOT a valid attestation — " + "; ".join(self.warnings)
            return (f"historical receipt for {self.instant}: valid for that instant "
                    f"only, not a statement about the current estate")
        if self.current_use:
            return (f"valid for CURRENT use: the estate was re-checked at "
                    f"{self.rechecked_at} against this receipt's scan and matches")
        if not self.historical:
            return "NOT a valid attestation — " + "; ".join(self.warnings)
        return (f"historical attestation as of {self.instant} only: not valid for "
                f"current use until re-attested with every estate source")


def assess_receipt(receipt: dict, live: dict | None = None) -> ReceiptAssessment:
    """Both decisions for a SIGNATURE-VALID receipt, in one place so every
    verification surface (CLI attest, verify-receipt, the MCP tool) applies
    the same rules. Never raises: malformed fields are a refusal with a
    reason, on exactly the arbitrary-JSON input class verification is for.

    `live` is a freshness block computed NOW against the receipt's own scan
    (services.freshness.check_freshness(scan_id=receipt["scan_id"], …)).
    Current reliance needs it: the receipt's embedded freshness says what
    was true when it was ISSUED, and a receipt issued fresh stayed "current"
    after the model it certified was edited. Offline verification
    establishes signature integrity and historical validity only.
    """
    is_as_of = bool(receipt.get("as_of"))
    describes = "as_of" if is_as_of else "current"
    instant = receipt.get("as_of") if is_as_of else receipt.get("verified_as_of")
    freshness = receipt.get("freshness")
    if freshness is not None and not isinstance(freshness, dict):
        return ReceiptAssessment(describes, instant, False, False, (
            f"freshness block is malformed ({type(freshness).__name__}, not an "
            f"object) — cannot evaluate decay; re-attest",))
    if not isinstance(receipt.get("status"), str):
        return ReceiptAssessment(describes, instant, False, False,
                                 ("receipt carries no status field — it certifies nothing",))

    fresh_status = (freshness or {}).get("status")
    drift = (freshness or {}).get("estate_drift") or {}
    drifted = any(drift.get(k) for k in ("changed", "added", "removed")) or any(
        (freshness or {}).get(k) for k in ("changed", "added", "removed"))

    if is_as_of:
        # A receipt ABOUT a past instant is valid for that instant and never
        # for current use. Current-estate freshness is disclosure only.
        if fresh_status == "stale" or (fresh_status == "partial" and drifted):
            note = f"the CURRENT estate has changed since (freshness `{fresh_status}`)"
        elif fresh_status == "unknown":
            note = "freshness of the CURRENT estate could not be determined"
        elif fresh_status == "fresh":
            note = ("the current estate still matches, but attest without --as-of "
                    "for a current answer")
        else:
            note = "it says nothing about the current estate"
        return ReceiptAssessment(describes, instant, True, False, (
            f"this receipt describes {instant}; it is valid for that instant only "
            f"and not for current use — {note}",))

    # A CURRENT receipt is a true record of verified_as_of either way; it may
    # be relied on now only if someone looks NOW: every scanned source
    # re-hashed against the snapshot of the receipt's own scan, unchanged.
    if live is not None:
        return _assess_live(receipt, describes, instant, live)
    if fresh_status == "fresh":
        checked = (freshness or {}).get("checked_at") or "issuance"
        return ReceiptAssessment(describes, instant, True, False, (
            f"historical attestation only, not valid for current use without a live "
            f"check: its freshness block says the estate matched when it was issued "
            f"({checked}), which is not a statement about now. Verify with the estate "
            f"sources (`semantix verify-receipt --db … --sql-dir/--dbt/…`, or an MCP "
            f"server configured with them) to rely on it now",))
    if fresh_status == "stale" or (fresh_status == "partial" and drifted):
        warning = (f"this receipt's own freshness block reports `{fresh_status}` with "
                   "recorded drift — the estate has moved since it was issued; "
                   "re-attest before relying on it")
    elif fresh_status == "unknown":
        warning = ("a freshness check was attempted and could not answer — "
                   "re-attest with estate sources")
    elif fresh_status == "partial":
        unchecked = (freshness or {}).get("unchecked_systems") or []
        warning = ("historical attestation only, not valid for current use: freshness "
                   "is `partial` — systems not re-checked: "
                   + (", ".join(unchecked) or "not recorded")
                   + ". Re-attest with every estate source to rely on it now")
    else:
        warning = ("historical attestation only, not valid for current use: the estate "
                   "was not re-checked when this receipt was issued, so nothing shows "
                   "the definition still matches what runs. Re-attest with the estate "
                   "sources (or from an MCP server configured with them) to rely on "
                   "it now")
    return ReceiptAssessment(describes, instant, True, False, (warning,))


def _assess_live(receipt: dict, describes: str, instant, live: dict) -> ReceiptAssessment:
    """Current reliance from a live re-check bound to the receipt's scan."""
    rechecked = live.get("checked_at") if isinstance(live, dict) else None
    if not isinstance(live, dict) or live.get("scan_id") != receipt.get("scan_id"):
        return ReceiptAssessment(describes, instant, True, False, (
            "the live estate check was not made against this receipt's scan — "
            "current reliance is not established",), rechecked)
    from semantix.extraction.proof import POLICY_ID

    issued_under = receipt.get("proof_policy") or "before semantix-proof/2"
    if receipt.get("proof_policy") != POLICY_ID:
        return ReceiptAssessment(describes, instant, True, False, (
            f"issued under proof policy {issued_under}; the policy in force is "
            f"{POLICY_ID} — rescan and re-attest to rely on it now",), rechecked)
    status = live.get("status")
    if live.get("proof_policy"):
        return ReceiptAssessment(describes, instant, True, False, (live["note"],), rechecked)
    if status == "fresh":
        return ReceiptAssessment(describes, instant, True, True, (), rechecked)
    drift = live.get("estate_drift") or {}
    if status == "stale":
        moved = [f"{k} {', '.join(v)}" for k, v in drift.items() if v]
        warning = (f"the estate has changed since this receipt's scan "
                   f"({'; '.join(moved) or 'drift recorded'}) — re-scan and re-attest")
    elif status == "partial":
        warning = ("re-checked systems match, but not every scanned system was "
                   "re-checked: " + (", ".join(live.get("unchecked_systems") or [])
                                     or "not recorded")
                   + " — verify with every estate source")
    else:
        warning = ("the live estate check could not answer"
                   + (f" ({live.get('note')})" if live.get("note") else ""))
    return ReceiptAssessment(describes, instant, True, False, (warning,), rechecked)


def receipt_trust(receipt: dict, live: dict | None = None) -> tuple[bool, list[str]]:
    """(valid for the use the receipt states, warnings). Kept for callers
    that ask one question; anything deciding CURRENT reliance must read
    assess_receipt(...).current_use, which is never true for an as-of
    receipt nor without a live check."""
    a = assess_receipt(receipt, live)
    return a.valid_for_stated_use, list(a.warnings)
