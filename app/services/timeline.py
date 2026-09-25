"""洽谈时间线组装服务：游标分页结果 -> 带审计链接的响应模型。"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .. import crud, schemas
from ..config import settings
from ..pagination import encode_cursor

# 录入时间晚于业务发生时间超过该宽限期，才标记为迟到补录。
# 当日整理纪要属于正常作业；跨天补登才视为“迟到补录”。
LATE_RECORD_GRACE = timedelta(hours=24)


def status_logs_url(project_id: int) -> str:
    return f"{settings.API_V1_PREFIX}/projects/{project_id}/status-logs"


def status_log_url(project_id: int, log_id: int) -> str:
    return f"{status_logs_url(project_id)}/{log_id}"


def _nearest_log(logs, held_at: datetime):
    """logs 已按 changed_at 倒序；取第一条 changed_at <= held_at 的日志。"""
    for log in logs:
        if log.changed_at is not None and log.changed_at <= held_at:
            return log
    return None


def build_negotiation_timeline(
    db: Session,
    *,
    limit: int,
    fingerprint: str,
    project_id: Optional[int] = None,
    intent_id: Optional[int] = None,
    cursor_held_at: Optional[datetime] = None,
    cursor_id: Optional[int] = None,
):
    rows, has_more = crud.list_negotiation_timeline(
        db,
        limit=limit,
        project_id=project_id,
        intent_id=intent_id,
        cursor_held_at=cursor_held_at,
        cursor_id=cursor_id,
    )

    project_ids = sorted({row.project_id for row in rows})
    logs_by_project = crud.map_nearest_status_logs(db, project_ids)

    items = []
    for neg, pid, project_name, project_status in rows:
        logs = logs_by_project.get(pid, [])
        nearest = _nearest_log(logs, neg.held_at)
        is_late = (
            neg.recorded_at is not None
            and neg.recorded_at - neg.held_at > LATE_RECORD_GRACE
        )
        items.append(
            schemas.NegotiationTimelineItem(
                id=neg.id,
                intent_id=neg.intent_id,
                round=neg.round,
                title=neg.title,
                held_at=neg.held_at,
                recorded_at=neg.recorded_at,
                is_late=is_late,
                location=neg.location,
                host=neg.host,
                participants=neg.participants,
                key_topics=neg.key_topics,
                consensus=neg.consensus,
                disagreements=neg.disagreements,
                next_steps=neg.next_steps,
                next_meeting_date=neg.next_meeting_date,
                minutes_author=neg.minutes_author,
                project_id=pid,
                project_name=project_name,
                project_status=project_status,
                project_status_logs_url=status_logs_url(pid),
                nearest_status_log=nearest,
                nearest_status_log_url=(
                    status_log_url(pid, nearest.id) if nearest else None
                ),
            )
        )

    next_cursor = None
    if has_more and rows:
        last_neg = rows[-1][0]
        next_cursor = encode_cursor(
            last_neg.held_at.isoformat(), last_neg.id, fingerprint
        )

    return schemas.NegotiationTimelinePage(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
        audit_links={pid: status_logs_url(pid) for pid in project_ids},
    )
