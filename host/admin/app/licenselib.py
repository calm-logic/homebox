"""License verification for Homebox Premium.

The control plane issues a signed license with every create/join/heartbeat.
The license object looks like:

    {valid, plan ("free"|"premium"|"dev"), max_nodes, node_count,
     features (["cluster","cloud-mirror"]), issued_at, expires_at,
     grace_days: 14, token: "hbl.<b64url(payload_json)>.<b64url(ed25519_sig)>"}

The token's payload is canonical JSON (sorted keys, compact separators) of
{cluster_id, plan, max_nodes, features, issued_at, expires_at}; the signature
is Ed25519 over the exact b64url payload segment BYTES. We fetch the signing
public key from GET {control_plane}/v1/license-key and pin it trust-on-first-use
so a later control-plane compromise can't silently re-key a running cluster.

Enforcement is deliberately gentle (see clusterlib.cluster_loop): an expired
license (beyond grace) stops NEW deploys/subscriptions but never drains a
serving node or tears down the mesh. Licenses WITHOUT a token (legacy/dev
control planes) are treated as legacy-valid so old clusters keep working.
"""

import base64
import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("homebox.license")

LICENSE_PUBKEY_KEY = "license_pubkey"   # settings key: pinned signing key (TOFU)
GRACE_DAYS_DEFAULT = 14

# Fields the token payload signs; the outer license dict must agree with these.
_SIGNED_FIELDS = ("cluster_id", "plan", "max_nodes", "features", "issued_at", "expires_at")


# ───── b64url helpers ─────────────────────────────────────────────────────────


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


# ───── signing-key pin (trust on first use) ───────────────────────────────────


async def fetch_license_key(session: AsyncSession, control_plane_url: str) -> str | None:
    """GET /v1/license-key and return the signing public key (hex), pinning it on
    first fetch. If a later fetch mismatches the pin, warn loudly and keep the
    pinned key (a rotated CP shouldn't be able to silently re-key us).

    Returns the key we trust (pinned, possibly just-pinned), or None when the
    control plane has no license-key endpoint (old CP → legacy-valid path)."""
    # Local import to avoid a config import cycle at module load.
    from . import clusterlib

    pinned = await clusterlib._get_setting(session, LICENSE_PUBKEY_KEY)
    pinned_hex = pinned.get("public_key") if isinstance(pinned, dict) else None

    try:
        url = control_plane_url.rstrip("/") + "/v1/license-key"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url)
        if r.status_code == 404:
            return pinned_hex  # old control plane; no signing key to verify against
        r.raise_for_status()
        data = r.json()
        fetched_hex = (data.get("public_key") or "").strip().lower()
        key_id = data.get("key_id")
    except (httpx.HTTPError, ValueError) as e:
        log.warning("license key fetch failed (using pinned=%s): %s", bool(pinned_hex), e)
        return pinned_hex

    if not fetched_hex:
        return pinned_hex
    if pinned_hex and fetched_hex != pinned_hex:
        log.warning(
            "license signing key MISMATCH — control plane returned a different key "
            "(key_id=%s) than the pinned one; keeping the pinned key", key_id,
        )
        return pinned_hex
    if not pinned_hex:
        await clusterlib._set_setting(session, LICENSE_PUBKEY_KEY, {
            "public_key": fetched_hex, "key_id": key_id, "algo": data.get("algo"),
        })
        await session.commit()
        log.info("pinned license signing key (key_id=%s)", key_id)
    return fetched_hex


# ───── verification ───────────────────────────────────────────────────────────


def verify_license(license_dict: dict[str, Any] | None, pubkey_hex: str | None) -> tuple[bool, str]:
    """Verify a license's Ed25519 token against the pinned signing key.

    Returns (verified, reason). verified=True only when the signature checks out
    AND the token payload agrees with the outer dict AND expiry is sane. A
    license WITHOUT a token is (False, "legacy") — the caller treats that as
    legacy-valid so pre-token control planes keep working. A tampered/mismatched
    token is (False, "<why>")."""
    if not isinstance(license_dict, dict):
        return False, "no-license"
    token = license_dict.get("token")
    if not token:
        return False, "legacy"          # unsigned license → legacy-valid upstream
    if not pubkey_hex:
        return False, "unpinned"        # can't verify without a signing key
    parts = str(token).split(".")
    if len(parts) != 3 or parts[0] != "hbl":
        return False, "malformed-token"
    _, payload_seg, sig_seg = parts

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        sig = _b64url_decode(sig_seg)
        pub.verify(sig, payload_seg.encode("ascii"))  # sig is over the b64url segment bytes
    except InvalidSignature:
        return False, "bad-signature"
    except Exception as e:  # noqa: BLE001 — malformed key/sig/hex
        return False, f"verify-error: {e}"

    try:
        payload = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError) as e:
        return False, f"bad-payload: {e}"

    # The signed payload must agree with what the outer dict advertises.
    for field in _SIGNED_FIELDS:
        if field == "cluster_id":
            continue  # not always present on the outer dict; trust the signed one
        if field in license_dict and payload.get(field) != license_dict.get(field):
            return False, f"payload-mismatch: {field}"

    exp = payload.get("expires_at")
    if exp is not None:
        exp_ts = _to_epoch(exp)
        if exp_ts is None:
            return False, "bad-expires_at"
    return True, "verified"


def _to_epoch(val: Any) -> float | None:
    """Accept an expiry as unix seconds (int/float) or ISO-8601 string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        try:
            return float(s)
        except ValueError:
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def license_status(state: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the effective license status from cluster state.

    Reads the cached license (state["license"]) and the verification verdict
    stashed at license-arrival time (state["license_verified"]). Returns:
        {plan, features, valid, verified, expires_at, in_grace, expired}
    where in_grace = now within [expires_at, expires_at + grace_days*86400) and
    expired = beyond that window. A legacy/unsigned license is valid but
    verified=False and never counted expired unless it carries a real expiry."""
    lic = (state or {}).get("license") if isinstance(state, dict) else None
    lic = lic if isinstance(lic, dict) else {}
    verified = bool((state or {}).get("license_verified")) if isinstance(state, dict) else False

    plan = lic.get("plan") or "free"
    features = lic.get("features") or []
    # An unsigned/legacy license is still "valid" (don't break old clusters);
    # its own `valid` flag (when present) wins.
    valid = bool(lic.get("valid", True))
    grace_days = lic.get("grace_days")
    grace_days = grace_days if isinstance(grace_days, (int, float)) else GRACE_DAYS_DEFAULT

    exp_raw = lic.get("expires_at")
    exp_ts = _to_epoch(exp_raw)
    now = time.time()
    in_grace = False
    expired = False
    if exp_ts is not None:
        grace_end = exp_ts + grace_days * 86400
        if now >= grace_end:
            expired = True
        elif now >= exp_ts:
            in_grace = True

    return {
        "plan": plan,
        "features": list(features),
        "valid": valid and not expired,
        "verified": verified,
        "expires_at": exp_raw,
        "in_grace": in_grace,
        "expired": expired,
    }


async def record_license_verification(
    session: AsyncSession, state: dict[str, Any], control_plane_url: str,
) -> tuple[bool, str]:
    """Verify state["license"] against the pinned signing key and stash the
    verdict on the state dict (state["license_verified"] / ["license_reason"]).
    Call this wherever a fresh license lands (create/join/heartbeat). Caller is
    responsible for persisting `state`. Best-effort: network/verify errors just
    leave verified=False. Legacy (unsigned) licenses stay legacy-valid.

    To keep heartbeats cheap we only hit the control plane for the signing key
    when it isn't pinned yet (trust-on-first-use); once pinned we verify against
    the pin without a network round-trip."""
    from . import clusterlib
    pinned = await clusterlib._get_setting(session, LICENSE_PUBKEY_KEY)
    pubkey = pinned.get("public_key") if isinstance(pinned, dict) else None
    if not pubkey:
        try:
            pubkey = await fetch_license_key(session, control_plane_url)
        except Exception as e:  # noqa: BLE001
            log.warning("license key fetch errored: %s", e)
            pubkey = None
    verified, reason = verify_license(state.get("license"), pubkey)
    state["license_verified"] = verified
    state["license_reason"] = reason
    if reason not in ("verified", "legacy", "no-license"):
        log.warning("license verification: %s", reason)
    return verified, reason
