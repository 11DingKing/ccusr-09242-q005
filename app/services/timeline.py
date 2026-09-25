"""洽谈时间线：按业务发生时间的稳定游标分页。

设计要点：

- 排序键固定为 ``(held_at, id)``：``held_at`` 是洽谈的业务发生时间，
  ``id`` 是自增稳定标识，作为同一时刻多条记录的并列决胜键，
  因此同秒补录的记录顺序也完全确定，任何翻页组合都不会重复或遗漏。
- 游标是无状态的 base64 令牌，只记录“上一页最后一条”的排序键和筛选指纹，
  服务重启后仍然有效；筛选条件与游标内指纹不一致时抛出
  :class:`FilterMismatchCursorError`，游标本身无法解析时抛出
  :class:`InvalidCursorError`。
- 迟到补录（录入时间晚于业务发生时间超过阈值）仍按 ``held_at``
  落在时间线上的原始位置，仅通过 ``is_late_recorded`` 标记提示。
"""

import base64
import hashlib
import json
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from .. import models


# 录入时间晚于业务发生时间超过 1 天（24 小时）视为迟到补录
LATE_RECORDING_THRESHOLD_DAYS = 1

_CURSOR_VERSION = 1


class CursorError(ValueError):
    """游标相关错误的基类。"""


class InvalidCursorError(CursorError):
    """游标格式损坏或内容无法解析。"""


class FilterMismatchCursorError(CursorError):
    """游标生成时的筛选条件与本次请求不一致，游标已失效。"""


def build_filter_fingerprint(filters: dict) -> str:
    """根据筛选条件生成稳定指纹；条件相同指纹才相同。"""
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def encode_cursor(held_at: datetime, record_id: int, fingerprint: str) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "t": held_at.isoformat(),
        "id": record_id,
        "f": fingerprint,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> Tuple[datetime, int]:
    """解析游标并校验筛选指纹，返回 (held_at, id)。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InvalidCursorError(str(exc))
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("unsupported cursor version")
    try:
        held_at = datetime.fromisoformat(payload["t"])
        record_id = int(payload["id"])
        cursor_fingerprint = str(payload["f"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError(str(exc))
    if cursor_fingerprint != fingerprint:
        raise FilterMismatchCursorError("filter fingerprint mismatch")
    return held_at, record_id


def is_late_recorded(record: models.NegotiationRecord) -> bool:
    """根据业务发生时间与录入时间判断是否迟到补录。"""
    recorded_at = record.recorded_at or record.created_at
    if not recorded_at or not record.held_at:
        return False
    return (
        recorded_at - record.held_at
    ).total_seconds() > LATE_RECORDING_THRESHOLD_DAYS * 86400


def list_negotiation_timeline(
    db: Session,
    *,
    project_id: int,
    intent_id: Optional[int] = None,
    round_no: Optional[int] = None,
    late_only: bool = False,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Tuple[List[models.NegotiationRecord], bool, Optional[str], str]:
    """按业务发生时间游标分页查询项目下的洽谈记录。

    返回 ``(records, has_more, next_cursor, fingerprint)``：
    多取一条判断是否还有下一页，因此返回给调用方的记录数不超过 limit。
    """
    filters = {
        "project_id": project_id,
        "intent_id": intent_id,
        "round": round_no,
        "late_only": late_only,
        "order": "held_at:asc,id:asc",
    }
    fingerprint = build_filter_fingerprint(filters)

    query = (
        db.query(models.NegotiationRecord)
        .join(
            models.CooperationIntent,
            models.NegotiationRecord.intent_id == models.CooperationIntent.id,
        )
        .filter(models.CooperationIntent.project_id == project_id)
    )
    if intent_id is not None:
        query = query.filter(models.NegotiationRecord.intent_id == intent_id)
    if round_no is not None:
        query = query.filter(models.NegotiationRecord.round == round_no)
    if late_only:
        entry_time = func.coalesce(
            models.NegotiationRecord.recorded_at,
            models.NegotiationRecord.created_at,
        )
        query = query.filter(
            func.julianday(entry_time) - func.julianday(models.NegotiationRecord.held_at)
            > LATE_RECORDING_THRESHOLD_DAYS
        )

    if cursor:
        held_at, record_id = decode_cursor(cursor, fingerprint)
        # 严格大于上一页最后一条的排序键 (held_at, id)，保证不重不漏
        query = query.filter(
            or_(
                models.NegotiationRecord.held_at > held_at,
                and_(
                    models.NegotiationRecord.held_at == held_at,
                    models.NegotiationRecord.id > record_id,
                ),
            )
        )

    query = query.order_by(
        models.NegotiationRecord.held_at.asc(),
        models.NegotiationRecord.id.asc(),
    ).limit(limit + 1)

    rows = query.all()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1].held_at, page[-1].id, fingerprint)
        if has_more
        else None
    )
    return page, has_more, next_cursor, fingerprint
