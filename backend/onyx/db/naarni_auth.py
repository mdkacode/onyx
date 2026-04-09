"""Database operations for Naarni user authentication tokens."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.db.models import NaarniUserToken


def get_naarni_token_for_user(
    db_session: Session,
    user_id: UUID,
) -> NaarniUserToken | None:
    """Fetch the Naarni token record for a given Onyx user."""
    stmt = select(NaarniUserToken).where(NaarniUserToken.user_id == user_id)
    return db_session.execute(stmt).scalar_one_or_none()


def upsert_naarni_token(
    db_session: Session,
    user_id: UUID,
    phone_number: str,
    naarni_device_id: int,
    access_token: str,
    refresh_token: str | None,
) -> NaarniUserToken:
    """Create or update the Naarni token for a user."""
    existing = get_naarni_token_for_user(db_session, user_id)

    if existing:
        existing.phone_number = phone_number
        existing.naarni_device_id = naarni_device_id
        existing.access_token = access_token  # type: ignore[assignment]
        existing.refresh_token = refresh_token  # type: ignore[assignment]
        db_session.commit()
        return existing

    token_record = NaarniUserToken(
        user_id=user_id,
        phone_number=phone_number,
        naarni_device_id=naarni_device_id,
        access_token=access_token,  # type: ignore[arg-type]
        refresh_token=refresh_token,  # type: ignore[arg-type]
    )
    db_session.add(token_record)
    db_session.commit()
    return token_record


def delete_naarni_token(
    db_session: Session,
    user_id: UUID,
) -> bool:
    """Remove the Naarni token for a user (disconnect). Returns True if deleted."""
    existing = get_naarni_token_for_user(db_session, user_id)
    if existing:
        db_session.delete(existing)
        db_session.commit()
        return True
    return False
