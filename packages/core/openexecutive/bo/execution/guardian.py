"""Guardian linkage for delegated execution (VAL4-01 REM-01).

Two faces of the same credential (``bo.exec.guardian_secret_ref`` env var,
never persisted):

* ``assert_effect_authorized`` — the pre-effect boundary. When
  ``bo.exec.guardian_endpoint`` is set, the CURRENT mandate status is
  re-read from ``GET /v1/mandates/{ref}/status`` immediately before each
  external effect. A cached snapshot would let a revoked mandate keep
  producing effects, so every boundary call hits Guardian live:

  - ``REVOKED``/``EXPIRED``/unknown mandate → ``GuardianDeniedError``
    (the run fails or goes to reconciliation — the effect never runs);
  - Guardian unreachable/5xx/timeout → ``GuardianUnavailableError``
    (the run PAUSES — recoverable, resumable by the operator);
  - ``bo.exec.guardian_auth_required=on`` makes missing binding/credential
    deny instead of warn: an unbound mandate cannot bypass the boundary.

* ``post_execution_event`` — the outbox delivery path for
  ``kind="execution"`` envelopes. Posts the persisted envelope
  byte-identically to ``POST /v1/execution-events``:

  - 202 ``RECEIVED`` / 200 ``DUPLICATE`` → delivered;
  - 401/403/409/413/422 → ``GuardianPermanentError`` (dead-letter —
    retrying the same bytes can never succeed; the conflict stays
    visible in the outbox);
  - network/timeout/5xx + receiver-side ``exec_control_disabled`` /
    ``exec_ingest_disabled`` → ``GuardianTransientError`` (stays
    pending for the next cycle).

No caching layer: the window between authorization and effect is already
minimal; a cache would silently widen it. The residual race (revocation
landing between the check and the provider call) is documented — it is
bounded by the provider call latency, not by a TTL.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ACK_OK = {"RECEIVED", "DUPLICATE", "ACCEPTED", "OK"}

# Receiver-side 4xx/5xx details that mean "module temporarily off" —
# the evidence must NOT dead-letter on a receiver toggle.
_RECEIVER_OFF_DETAILS = frozenset(
    {"exec_control_disabled", "exec_ingest_disabled"}
)


class GuardianUnavailableError(Exception):
    """Guardian could not be reached or answered transiently — retryable."""


class GuardianDeniedError(Exception):
    """Guardian refused the effect — permanent until the mandate changes.

    ``kind``: ``revoked`` | ``expired`` | ``not_found`` | ``forbidden`` |
    ``misconfigured`` | ``unbound`` | ``not_active``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GuardianTransientError(Exception):
    """Delivery failed retryably — the envelope stays pending."""


class GuardianPermanentError(Exception):
    """Delivery failed permanently — the envelope dead-letters visibly."""


def _setting(tenant: str, key: str, default: Any, db_path: Path | None) -> Any:
    try:
        from openexecutive.bo.settings import store as settings_store

        return settings_store.get_effective_value(
            tenant, key, db_path=db_path
        )
    except Exception:  # noqa: BLE001 — settings hiccup = safe default
        return default


def _link_config(
    tenant: str, db_path: Path | None
) -> tuple[str, str | None, float, bool]:
    """(endpoint, token-or-None, timeout_s, auth_required).

    ``BO_TELEMETRY_ENDPOINT``/``BO_TELEMETRY_TOKEN`` are honored as the
    deployment-level transport contract (the gate sets them directly);
    the persisted ``bo.exec.guardian_*`` settings fill whatever the env
    leaves unset. ``BO_TELEMETRY_ENDPOINT`` is a full URL — the base is
    recovered by stripping ``/v1/...`` for the mandate-status calls."""
    endpoint = str(
        _setting(tenant, "bo.exec.guardian_endpoint", "", db_path)
    ).rstrip("/")
    env_endpoint = os.environ.get("BO_TELEMETRY_ENDPOINT", "").rstrip("/")
    if not endpoint and env_endpoint:
        endpoint = env_endpoint.split("/v1/")[0]
    secret_ref = str(
        _setting(
            tenant, "bo.exec.guardian_secret_ref",
            "BO_GUARDIAN_TOKEN", db_path,
        )
    )
    token = (
        os.environ.get(secret_ref)
        or os.environ.get("BO_TELEMETRY_TOKEN")
        or None
    )
    timeout_s = float(
        _setting(tenant, "bo.exec.guardian_timeout_s", 5, db_path)
    )
    required = bool(
        _setting(
            tenant, "bo.exec.guardian_auth_required", False, db_path
        )
    )
    return endpoint, token, timeout_s, required


def _request(
    method: str, url: str, token: str, timeout_s: float,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One HTTP round-trip. Returns (status, parsed-json-or-{}).
    ``urllib.error.HTTPError`` carries the status; everything else
    network-side raises ``GuardianUnavailableError``/``Transient`` via
    the callers' classification."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        raw = resp.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    return resp.status, parsed if isinstance(parsed, dict) else {}


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read())
        return str(body.get("detail") or body)[:200]
    except Exception:  # noqa: BLE001 — non-JSON error body
        return f"HTTP {exc.code}"


# --------------------------------------------------------------------------- #
# Pre-effect authorization — GET /v1/mandates/{ref}/status
# --------------------------------------------------------------------------- #

def assert_effect_authorized(
    tenant: str, mandate: Any, *, db_path: Path | None = None
) -> None:
    """Re-check the Guardian-held mandate status NOW, at the effect
    boundary. Returns silently when the link is not configured; raises
    ``GuardianDeniedError``/``GuardianUnavailableError`` otherwise.

    Guardian holds the distributed revocation — the local mandate row
    only proves the chain BOAgents issued; a mandate revoked centrally
    must stop effects even while the local row still looks active.
    """
    endpoint, token, timeout_s, required = _link_config(tenant, db_path)
    if not endpoint:
        return  # link not configured — local chain only (documented)
    guardian_ref = getattr(mandate, "guardian_ref", None)
    if not guardian_ref:
        if required:
            raise GuardianDeniedError(
                "unbound",
                "mandatul nu are guardian_ref — autorizarea Guardian e "
                "obligatorie",
            )
        return
    if not token:
        if required:
            raise GuardianDeniedError(
                "misconfigured",
                "secretul Guardian nu este setat în mediul procesului",
            )
        logger.warning(
            "guardian: endpoint configurat dar tokenul lipsește — "
            "verificarea la frontieră se sare (auth_required=off)"
        )
        return
    url = f"{endpoint}/v1/mandates/{guardian_ref}/status"
    try:
        status, body = _request("GET", url, token, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code >= 500 or detail in _RECEIVER_OFF_DETAILS:
            raise GuardianUnavailableError(
                f"HTTP {exc.code}: {detail}"
            ) from exc
        if exc.code in (401, 403):
            raise GuardianDeniedError("forbidden", detail) from exc
        if exc.code == 404:
            raise GuardianDeniedError(
                "not_found",
                f"mandatul {guardian_ref} nu există în Guardian",
            ) from exc
        raise GuardianDeniedError(
            "forbidden", f"HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — DNS/connect/timeout/TLS
        raise GuardianUnavailableError(str(exc)[:200]) from exc
    mandate_status = str(body.get("status") or "")
    if mandate_status == "REVOKED":
        raise GuardianDeniedError(
            "revoked", f"mandatul {guardian_ref} este REVOKED în Guardian"
        )
    expires_raw = body.get("expiresAt")
    expired = mandate_status == "EXPIRED"
    if not expired and expires_raw:
        try:
            expired = datetime.fromisoformat(
                str(expires_raw).replace("Z", "+00:00")
            ) <= datetime.now(UTC)
        except ValueError:
            expired = False  # unparseable timestamp — status field governs
    if expired:
        raise GuardianDeniedError(
            "expired", f"mandatul {guardian_ref} a expirat în Guardian"
        )
    if mandate_status != "ACTIVE":
        raise GuardianDeniedError(
            "not_active",
            f"mandatul {guardian_ref} are starea "
            f"{mandate_status or 'lipsă'} în Guardian",
        )


# --------------------------------------------------------------------------- #
# Event delivery — POST /v1/execution-events
# --------------------------------------------------------------------------- #

def post_execution_event(
    tenant: str, envelope: dict[str, Any], *, db_path: Path | None = None
) -> dict[str, Any]:
    """Deliver one persisted execution envelope to Guardian.

    The caller (outbox delivery) maps outcomes:
    ``GuardianTransientError`` → stays pending; ``GuardianPermanentError``
    → dead-letter with the receiver's detail; any ack dict → delivered.
    """
    endpoint, token, timeout_s, _required = _link_config(tenant, db_path)
    if not endpoint:
        raise GuardianTransientError(
            "bo.exec.guardian_endpoint neconfigurat"
        )
    if not token:
        raise GuardianPermanentError(
            "secretul Guardian nu este setat în mediul procesului"
        )
    url = os.environ.get("BO_TELEMETRY_ENDPOINT", "").rstrip("/") or (
        f"{endpoint}/v1/execution-events"
    )
    try:
        status, body = _request(
            "POST", url, token, timeout_s, body=envelope,
        )
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code >= 500 or exc.code == 429 or (
            exc.code == 404 and detail in _RECEIVER_OFF_DETAILS
        ):
            raise GuardianTransientError(
                f"HTTP {exc.code}: {detail}"
            ) from exc
        raise GuardianPermanentError(
            f"HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — network/timeout/TLS
        raise GuardianTransientError(str(exc)[:200]) from exc
    if status >= 500:
        raise GuardianTransientError(f"HTTP {status}")
    ack_status = str(body.get("status") or "")
    if ack_status not in _ACK_OK:
        raise GuardianPermanentError(
            f"ack neașteptat: {ack_status or f'HTTP {status}'}"
        )
    return body
