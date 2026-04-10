"""Periodic background task that keeps Naarni access tokens fresh.

Naarni access tokens live for ~6 hours; refresh tokens live ~90 days. The
Fleet Data chat tool already retries once on 401 with an inline refresh,
but that leaves a window where the *first* query after the token expires
pays a latency hit. This task proactively refreshes tokens before they
expire so fleet queries never see a stale-token error.

Strategy:
  - Run every 15 minutes (beat schedule entry in beat_schedule.py)
  - For each naarni_user_token row whose updated_at is older than
    REFRESH_AFTER_MINUTES, call `refresh_user_naarni_token`
  - Rows where refresh fails are left alone — the user will be asked to
    reconnect the next time they invoke the Fleet tool

Runs on the primary worker. Guarded by a Redis lock so two beat workers
can't double-refresh the same rows.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from celery import shared_task
from celery import Task
from redis.lock import Lock as RedisLock
from sqlalchemy import select

from onyx.background.celery.apps.app_base import task_logger
from onyx.configs.constants import CELERY_GENERIC_BEAT_LOCK_TIMEOUT
from onyx.configs.constants import OnyxCeleryTask
from onyx.configs.constants import OnyxRedisLocks
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.models import NaarniUserToken
from onyx.redis.redis_pool import get_redis_client
from onyx.server.features.naarni_auth.token_refresh import NaarniRefreshFailed
from onyx.server.features.naarni_auth.token_refresh import (
    refresh_user_naarni_token,
)


# Refresh any token whose row hasn't been touched in this long. The Naarni
# access token TTL is ~6 hours (21_600 s). We refresh after 5 hours to give
# ourselves a full hour of slack in case the beat task is delayed.
REFRESH_AFTER_MINUTES = 5 * 60


@shared_task(
    name=OnyxCeleryTask.CHECK_FOR_NAARNI_TOKEN_REFRESH,
    soft_time_limit=300,
    bind=True,
)
def check_for_naarni_token_refresh(self: Task, *, tenant_id: str) -> int:
    """Refresh Naarni tokens that are nearing expiry.

    Returns the number of tokens successfully refreshed (for logging /
    monitoring). Exceptions on individual rows are caught and logged so
    one bad token doesn't block the rest.
    """
    # noqa: ARG001 — self is required by @shared_task(bind=True)
    _ = self

    redis_client = get_redis_client(tenant_id=tenant_id)
    lock: RedisLock = redis_client.lock(
        OnyxRedisLocks.CHECK_NAARNI_TOKEN_REFRESH_BEAT_LOCK,
        timeout=CELERY_GENERIC_BEAT_LOCK_TIMEOUT,
    )
    if not lock.acquire(blocking=False):
        return 0

    refreshed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=REFRESH_AFTER_MINUTES)
        with get_session_with_current_tenant() as db_session:
            stale_rows = (
                db_session.execute(
                    select(NaarniUserToken).where(NaarniUserToken.updated_at < cutoff)
                )
                .scalars()
                .all()
            )

            if not stale_rows:
                return 0

            task_logger.info(
                "Naarni token refresh sweep: %d stale rows (tenant=%s)",
                len(stale_rows),
                tenant_id,
            )

            for row in stale_rows:
                try:
                    refresh_user_naarni_token(
                        db_session=db_session, user_id=row.user_id
                    )
                    refreshed += 1
                except NaarniRefreshFailed as e:
                    task_logger.warning(
                        "Skipping Naarni refresh for user %s: %s",
                        row.user_id,
                        e,
                    )
                except Exception:
                    task_logger.exception(
                        "Unexpected error refreshing Naarni token for user %s",
                        row.user_id,
                    )

    finally:
        if lock.owned():
            lock.release()

    if refreshed:
        task_logger.info("Naarni token refresh sweep finished: refreshed=%d", refreshed)
    return refreshed
