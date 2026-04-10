"""Naarni token refresh helpers.

Centralizes the logic for calling Naarni's `/api/v1/auth/token/refresh`
endpoint and updating the encrypted token row. Used by:
  - The FastAPI routes in api.py (for manual refresh + in-flight refresh)
  - The NaarniFleetTool (for automatic retry on 401)
  - The background Celery task (for proactive refresh before expiry)

All three callers funnel through `refresh_user_naarni_token` so there is
exactly one place that speaks to Naarni's refresh endpoint.
"""

import os
import re
from uuid import UUID
from uuid import uuid5

import requests
from sqlalchemy.orm import Session

from onyx.db.models import NaarniUserToken
from onyx.db.naarni_auth import get_naarni_token_for_user
from onyx.db.naarni_auth import upsert_naarni_token
from onyx.utils.logger import setup_logger

logger = setup_logger()

NAARNI_API_BASE_URL = os.environ.get("NAARNI_API_BASE_URL", "")

# Must match the namespace in api.py so device UUIDs are identical across
# OTP flow and refresh flow for a given deployment.
_DEVICE_UUID_NAMESPACE = UUID("7e2c3a19-4f8b-4d6e-a1c9-3b5f8e7d2a01")


def naarni_device_uuid() -> str:
    """Deterministic device UUID for this ONYX deployment.

    Same base URL always produces the same UUID, matching the one Naarni
    registered during the initial OTP flow.
    """
    return str(uuid5(_DEVICE_UUID_NAMESPACE, NAARNI_API_BASE_URL))


# ── Phone number normalization ────────────────────────────────────────────────
#
# Mirrors the behavior of website/src/services/api/authService.ts:
#   - strip optional "+91" prefix
#   - strip all non-digit characters
#   - require exactly 10 digits
#
# The Naarni backend's /auth/otp/generate endpoint expects a bare 10-digit
# phone number (no country code), per the Postman collection examples.

_PLUS_91_PREFIX_RE = re.compile(r"^\+?91")
_NON_DIGIT_RE = re.compile(r"\D")


def normalize_phone_number(raw: str) -> str:
    """Return a Naarni-ready 10-digit phone number, or raise ValueError.

    Accepts any of: "9999955555", "+91 99999 55555", "91-99999-55555".
    """
    if not raw:
        raise ValueError("Phone number is required.")
    stripped = _PLUS_91_PREFIX_RE.sub("", raw.strip())
    digits = _NON_DIGIT_RE.sub("", stripped)
    if len(digits) != 10:
        raise ValueError(
            f"Invalid phone number: expected 10 digits, got {len(digits)}."
        )
    return digits


# ── Token refresh ─────────────────────────────────────────────────────────────


def _decrypt(sensitive_value: object) -> str | None:
    """Unwrap an EncryptedString / SensitiveValue to its raw string value."""
    if sensitive_value is None:
        return None
    if hasattr(sensitive_value, "get_value"):
        return sensitive_value.get_value(apply_mask=False)  # type: ignore[attr-defined]
    return str(sensitive_value)


class NaarniRefreshFailed(Exception):
    """Raised when refreshing the Naarni access token fails.

    The caller can decide whether to surface this to the user, retry, or
    fall back to prompting a re-link.
    """


def refresh_user_naarni_token(
    db_session: Session,
    user_id: UUID,
) -> str:
    """Refresh a user's Naarni access token.

    1. Loads the encrypted refresh token from `naarni_user_token`.
    2. POSTs to Naarni `/api/v1/auth/token/refresh` with `{refresh_token}`.
    3. On success, writes the new access_token (+ refresh_token if rotated)
       back to the DB — reusing the same phone_number and device_id.
    4. Returns the new access token.

    Raises `NaarniRefreshFailed` on any failure (no refresh token, Naarni
    rejected it, network error, etc.). The caller should catch this and
    prompt the user to re-link their account.
    """
    if not NAARNI_API_BASE_URL:
        raise NaarniRefreshFailed("NAARNI_API_BASE_URL is not configured.")

    token_record: NaarniUserToken | None = get_naarni_token_for_user(
        db_session, user_id
    )
    if token_record is None:
        raise NaarniRefreshFailed(f"No Naarni token record for user {user_id}.")

    refresh_token = _decrypt(token_record.refresh_token)
    if not refresh_token:
        raise NaarniRefreshFailed(
            f"User {user_id} has no refresh token stored (reconnect required)."
        )

    device_uuid = naarni_device_uuid()

    try:
        resp = requests.post(
            f"{NAARNI_API_BASE_URL}/api/v1/auth/token/refresh",
            headers={
                "Content-Type": "application/json",
                "x-device-id": device_uuid,
            },
            json={"refresh_token": refresh_token},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        detail = ""
        try:
            body = e.response.json() if e.response is not None else {}
            detail = body.get("error_message") or body.get("errorMessage") or ""
        except Exception:
            pass
        logger.warning(
            "Naarni token refresh rejected (HTTP %s) for user %s: %s",
            status,
            user_id,
            detail,
        )
        raise NaarniRefreshFailed(
            f"Naarni refresh rejected ({status}): {detail or 'unknown'}"
        ) from e
    except requests.exceptions.RequestException as e:
        logger.warning("Naarni token refresh network error for user %s: %s", user_id, e)
        raise NaarniRefreshFailed(f"Naarni refresh network error: {e}") from e

    data = resp.json()
    new_access = data.get("access_token", "")
    # Naarni may or may not rotate the refresh token; keep the old one if
    # the response doesn't include a new one.
    new_refresh = data.get("refresh_token") or refresh_token

    if not new_access:
        raise NaarniRefreshFailed("Naarni refresh response missing access_token.")

    upsert_naarni_token(
        db_session=db_session,
        user_id=user_id,
        phone_number=token_record.phone_number,
        naarni_device_id=token_record.naarni_device_id,
        access_token=new_access,
        refresh_token=new_refresh,
    )

    logger.info("Refreshed Naarni token for user %s", user_id)
    return new_access
