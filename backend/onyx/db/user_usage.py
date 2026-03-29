"""Database queries for per-user token usage tracking."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID

from sqlalchemy import cast
from sqlalchemy import Date
from sqlalchemy import func
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.configs.constants import MessageType
from onyx.db.models import ChatMessage
from onyx.db.models import ChatSession
from onyx.db.models import User

# Rough cost estimate: $0.15 per 1M input tokens for gpt-4.1-mini
# = 0.015 cents per 1K tokens = 0.000015 cents per token
_COST_CENTS_PER_TOKEN = 0.000015


def _apply_time_filters(
    stmt: Select[tuple],
    period_from: datetime | None,
    period_to: datetime | None,
) -> Select[tuple]:
    if period_from is not None:
        stmt = stmt.where(ChatMessage.time_sent >= period_from)
    if period_to is not None:
        stmt = stmt.where(ChatMessage.time_sent <= period_to)
    return stmt


def _default_period(
    period_from: datetime | None,
    period_to: datetime | None,
    period_days: int = 30,
) -> tuple[datetime, datetime]:
    """Return concrete from/to datetimes, defaulting to the last N days."""
    now = datetime.now(timezone.utc)
    if period_to is None:
        period_to = now
    if period_from is None:
        period_from = now - timedelta(days=period_days)
    return period_from, period_to


def get_user_token_usage(
    db_session: Session,
    user_id: UUID,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> dict[str, object]:
    """Get token usage stats for a specific user within a time period."""
    period_from, period_to = _default_period(period_from, period_to)

    base = (
        select(
            func.coalesce(
                func.sum(ChatMessage.token_count).filter(
                    ChatMessage.message_type == MessageType.ASSISTANT
                ),
                0,
            ).label("total_tokens"),
            func.count(ChatMessage.id)
            .filter(ChatMessage.message_type == MessageType.USER)
            .label("message_count"),
            func.count(func.distinct(ChatMessage.chat_session_id)).label(
                "session_count"
            ),
        )
        .join(ChatSession, ChatMessage.chat_session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id)
    )
    base = _apply_time_filters(base, period_from, period_to)

    row = db_session.execute(base).one()
    total_tokens: int = int(row.total_tokens)

    return {
        "user_id": str(user_id),
        "total_tokens": total_tokens,
        "message_count": int(row.message_count),
        "session_count": int(row.session_count),
        "estimated_cost_cents": round(total_tokens * _COST_CENTS_PER_TOKEN, 4),
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
    }


def get_all_users_token_usage(
    db_session: Session,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> list[dict[str, object]]:
    """Get token usage for ALL users -- for the admin dashboard."""
    period_from, period_to = _default_period(period_from, period_to)

    base = (
        select(
            ChatSession.__table__.c.user_id.label("user_id"),
            User.__table__.c.email.label("email"),
            func.coalesce(
                func.sum(ChatMessage.token_count).filter(
                    ChatMessage.message_type == MessageType.ASSISTANT
                ),
                0,
            ).label("total_tokens"),
            func.count(ChatMessage.id)
            .filter(ChatMessage.message_type == MessageType.USER)
            .label("message_count"),
            func.count(func.distinct(ChatMessage.chat_session_id)).label(
                "session_count"
            ),
        )
        .join(ChatSession, ChatMessage.chat_session_id == ChatSession.id)
        .join(User, ChatSession.user_id == User.id)
        .group_by(ChatSession.__table__.c.user_id, User.__table__.c.email)
    )
    base = _apply_time_filters(base, period_from, period_to)

    rows = db_session.execute(base).all()

    results: list[dict[str, object]] = []
    for row in rows:
        total_tokens = int(row.total_tokens)
        results.append(
            {
                "user_id": str(row.user_id),
                "email": row.email,
                "total_tokens": total_tokens,
                "message_count": int(row.message_count),
                "session_count": int(row.session_count),
                "estimated_cost_cents": round(total_tokens * _COST_CENTS_PER_TOKEN, 4),
            }
        )

    # Sort by total_tokens descending so heaviest users appear first
    results.sort(key=lambda r: int(str(r["total_tokens"])), reverse=True)
    return results


def get_user_daily_breakdown(
    db_session: Session,
    user_id: UUID,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> list[dict[str, object]]:
    """Get daily token usage breakdown for a user."""
    period_from, period_to = _default_period(period_from, period_to)

    date_col = cast(ChatMessage.time_sent, Date).label("date")

    base = (
        select(
            date_col,
            func.coalesce(
                func.sum(ChatMessage.token_count).filter(
                    ChatMessage.message_type == MessageType.ASSISTANT
                ),
                0,
            ).label("tokens"),
            func.count(ChatMessage.id)
            .filter(ChatMessage.message_type == MessageType.USER)
            .label("messages"),
        )
        .join(ChatSession, ChatMessage.chat_session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id)
        .group_by(date_col)
        .order_by(date_col)
    )
    base = _apply_time_filters(base, period_from, period_to)

    rows = db_session.execute(base).all()

    return [
        {
            "date": row.date.isoformat(),
            "tokens": int(row.tokens),
            "messages": int(row.messages),
            "estimated_cost_cents": round(int(row.tokens) * _COST_CENTS_PER_TOKEN, 4),
        }
        for row in rows
    ]
