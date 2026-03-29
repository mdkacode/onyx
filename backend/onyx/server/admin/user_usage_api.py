"""Admin API endpoints for per-user token usage tracking."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from onyx.auth.users import current_admin_user
from onyx.db.engine.sql_engine import get_session
from onyx.db.models import User
from onyx.db.user_usage import get_all_users_token_usage
from onyx.db.user_usage import get_user_daily_breakdown
from onyx.db.user_usage import get_user_token_usage
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError

router = APIRouter(prefix="/admin/usage")


@router.get("/users")
def get_users_usage(
    period_days: int = 30,
    _: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    """Get token usage for all users within the specified period."""
    now = datetime.now(timezone.utc)
    period_from = now - timedelta(days=period_days)
    return get_all_users_token_usage(
        db_session=db_session,
        period_from=period_from,
        period_to=now,
    )


@router.get("/users/{user_id}")
def get_user_usage_detail(
    user_id: str,
    period_days: int = 30,
    _: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> dict[str, object]:
    """Get detailed token usage for a specific user."""
    try:
        uid = UUID(user_id)
    except ValueError:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            detail=f"Invalid user_id: {user_id}",
        )

    now = datetime.now(timezone.utc)
    period_from = now - timedelta(days=period_days)
    return get_user_token_usage(
        db_session=db_session,
        user_id=uid,
        period_from=period_from,
        period_to=now,
    )


@router.get("/users/{user_id}/daily")
def get_user_daily_usage(
    user_id: str,
    period_days: int = 30,
    _: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    """Get daily token usage breakdown for a specific user."""
    try:
        uid = UUID(user_id)
    except ValueError:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            detail=f"Invalid user_id: {user_id}",
        )

    now = datetime.now(timezone.utc)
    period_from = now - timedelta(days=period_days)
    return get_user_daily_breakdown(
        db_session=db_session,
        user_id=uid,
        period_from=period_from,
        period_to=now,
    )
