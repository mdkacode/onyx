"""API endpoints for linking a user's Naarni fleet account via phone + OTP.

Flow:
  1. POST /naarni-auth/request-otp   — registers device (if needed) + sends OTP
  2. POST /naarni-auth/verify-otp    — exchanges OTP for tokens, stores encrypted
  3. GET  /naarni-auth/status         — check if user has a linked Naarni account
  4. POST /naarni-auth/disconnect     — remove the linked account

The only required env var is NAARNI_API_BASE_URL. Device registration happens
automatically on first OTP request — no pre-configuration needed.
"""

import os
from uuid import UUID
from uuid import uuid5

import requests
from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.users import current_user
from onyx.db.engine.sql_engine import get_session
from onyx.db.models import User
from onyx.db.naarni_auth import delete_naarni_token
from onyx.db.naarni_auth import get_naarni_token_for_user
from onyx.db.naarni_auth import upsert_naarni_token
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.utils.logger import setup_logger

logger = setup_logger()

router = APIRouter(prefix="/naarni-auth")

NAARNI_API_BASE_URL = os.environ.get("NAARNI_API_BASE_URL", "")

# Fixed namespace for deterministic device UUID generation.
# uuid5(namespace, NAARNI_API_BASE_URL) always produces the same UUID for a
# given deployment, so the device is stable across process restarts.
_DEVICE_UUID_NAMESPACE = UUID("7e2c3a19-4f8b-4d6e-a1c9-3b5f8e7d2a01")

# In-memory cache — avoids re-registering on every OTP request within the
# same process lifetime.
_registered_device: dict[str, str | int] = {}


def _require_naarni_config() -> None:
    if not NAARNI_API_BASE_URL:
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE,
            "Naarni fleet integration is not configured. "
            "Set the NAARNI_API_BASE_URL environment variable.",
        )


def _ensure_device_registered() -> tuple[str, int]:
    """Auto-register a device with Naarni if we haven't already.

    Returns (device_uuid, device_id).
    """
    if _registered_device.get("uuid") and _registered_device.get("id"):
        return str(_registered_device["uuid"]), int(_registered_device["id"])

    # Deterministic: same base URL always produces the same device UUID
    device_uuid = str(uuid5(_DEVICE_UUID_NAMESPACE, NAARNI_API_BASE_URL))

    try:
        resp = requests.post(
            f"{NAARNI_API_BASE_URL}/api/v1/devices",
            headers={"Content-Type": "application/json"},
            json={
                "deviceUuid": device_uuid,
                "type": "MOBILE_APP",
                "status": "ACTIVE",
                "metadata": {
                    "appVersion": "1.0.0",
                    "platformId": "onyx-gyan",
                    "deviceModel": "Onyx Server",
                },
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()

        # Naarni returns the device info in body.body
        device_data = body.get("body", body)
        device_id = device_data.get("id")

        if not device_id:
            raise ValueError(f"No device ID in response: {body}")

        _registered_device["uuid"] = device_uuid
        _registered_device["id"] = device_id
        logger.info(f"Naarni device registered: uuid={device_uuid}, id={device_id}")
        return device_uuid, int(device_id)

    except requests.exceptions.HTTPError as e:
        # 200 with existing device is also fine
        if e.response is not None and e.response.status_code == 200:
            body = e.response.json()
            device_data = body.get("body", body)
            device_id = device_data.get("id")
            if device_id:
                _registered_device["uuid"] = device_uuid
                _registered_device["id"] = device_id
                return device_uuid, int(device_id)

        logger.error(f"Failed to register Naarni device: {e}")
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            "Failed to register device with Naarni. Please try again.",
        )
    except requests.exceptions.ConnectionError:
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE,
            "Cannot reach the Naarni service. Please try again later.",
        )


# ── Request / Response models ─────────────────────────────────────────────────


class RequestOtpRequest(BaseModel):
    phone_number: str


class RequestOtpResponse(BaseModel):
    success: bool
    message: str


class VerifyOtpRequest(BaseModel):
    phone_number: str
    otp: str


class VerifyOtpResponse(BaseModel):
    success: bool
    message: str


class NaarniAuthStatus(BaseModel):
    connected: bool
    phone_number: str | None = None


class DisconnectResponse(BaseModel):
    success: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/request-otp")
def request_otp(
    request: RequestOtpRequest,
    _user: User = Depends(current_user),
) -> RequestOtpResponse:
    """Step 1: Auto-register device + request OTP via SMS.

    Calls Naarni:
      POST /api/v1/devices       (if not already registered)
      POST /api/v1/auth/otp/generate
    """
    _require_naarni_config()

    device_uuid, device_id = _ensure_device_registered()

    try:
        resp = requests.post(
            f"{NAARNI_API_BASE_URL}/api/v1/auth/otp/generate",
            headers={
                "Content-Type": "application/json",
                "x-device-id": device_uuid,
                "x-platform": "WEB",
            },
            json={
                "contact": request.phone_number,
                "contactType": "PHONE",
                "deviceId": device_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 500
        detail = ""
        try:
            detail = e.response.json().get("errorMessage", "") if e.response else ""
        except Exception:
            pass
        logger.error(f"Naarni OTP request failed ({status}): {detail}")
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            f"Failed to send OTP: {detail or 'Naarni service error'}",
            status_code_override=status,
        )
    except requests.exceptions.ConnectionError:
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE,
            "Cannot reach the Naarni service. Please try again later.",
        )

    return RequestOtpResponse(
        success=True,
        message=f"OTP sent to {request.phone_number}. Please check your SMS.",
    )


@router.post("/verify-otp")
def verify_otp(
    request: VerifyOtpRequest,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> VerifyOtpResponse:
    """Step 2: Verify OTP and store Naarni tokens for this user.

    Calls Naarni: POST /api/v1/auth/token (x-www-form-urlencoded)
    """
    _require_naarni_config()

    device_uuid, device_id = _ensure_device_registered()

    try:
        resp = requests.post(
            f"{NAARNI_API_BASE_URL}/api/v1/auth/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "x-device-id": device_uuid,
            },
            data={
                "device_id": str(device_id),
                "grant_type": "phone",
                "otp": request.otp,
                "phone": request.phone_number,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 500
        detail = ""
        try:
            body = e.response.json() if e.response else {}
            detail = body.get("error_message") or body.get("errorMessage") or ""
        except Exception:
            pass
        logger.error(f"Naarni OTP verification failed ({status}): {detail}")

        if status == 401:
            raise OnyxError(
                OnyxErrorCode.UNAUTHENTICATED,
                detail or "Invalid or expired OTP. Please request a new one.",
            )
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            f"OTP verification failed: {detail or 'Naarni service error'}",
            status_code_override=status,
        )
    except requests.exceptions.ConnectionError:
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE,
            "Cannot reach the Naarni service. Please try again later.",
        )

    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            "Naarni returned an empty access token.",
        )

    upsert_naarni_token(
        db_session=db_session,
        user_id=user.id,
        phone_number=request.phone_number,
        naarni_device_id=device_id,
        access_token=access_token,
        refresh_token=refresh_token,
    )

    logger.info(
        f"Naarni account linked for user {user.id} (phone: {request.phone_number})"
    )

    return VerifyOtpResponse(
        success=True,
        message="Naarni account linked successfully. You can now ask about fleet data in chat.",
    )


@router.get("/status")
def get_auth_status(
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> NaarniAuthStatus:
    """Check if the current user has a linked Naarni account."""
    token_record = get_naarni_token_for_user(db_session, user.id)
    if token_record:
        return NaarniAuthStatus(
            connected=True,
            phone_number=token_record.phone_number,
        )
    return NaarniAuthStatus(connected=False)


@router.post("/disconnect")
def disconnect(
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> DisconnectResponse:
    """Remove the linked Naarni account."""
    deleted = delete_naarni_token(db_session, user.id)
    if deleted:
        logger.info(f"Naarni account unlinked for user {user.id}")
    return DisconnectResponse(success=True)
