"""洽谈时间线的稳定游标分页。

排序采用业务发生时间（``held_at`` 降序），同一时刻用记录稳定标识 ``id``
降序做并列决胜，保证任何页面组合都不会重复或遗漏记录。

游标为自包含的 base64(JSON) 结构，内部携带：

* ``v``       游标版本，便于未来排序规则演进；
* ``held_at`` 上一页最后一条记录的业务发生时间（ISO8601，秒级存储也可安全往返）；
* ``id``      上一页最后一条记录的稳定标识，并列决胜值；
* ``f``       筛选指纹，筛选条件变化使旧游标失效时返回明确错误。

游标不依赖总数或偏移量，因此并发新增、迟到补录或服务重启后都可以继续翻页。
"""

import base64
import hashlib
import json
from typing import Any, Mapping, Optional, Tuple

CURSOR_VERSION = 1


class CursorError(ValueError):
    """游标无法解析或已失效。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(token: str) -> bytes:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding)


def build_filter_fingerprint(filters: Mapping[str, Any]) -> str:
    """根据参与查询的筛选条件生成稳定指纹。

    只有真正收窄结果集的条件参与指纹，``limit`` 等翻页参数不参与。
    """
    canonical = json.dumps(
        {key: filters[key] for key in sorted(filters)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def encode_cursor(held_at_iso: str, record_id: int, fingerprint: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "held_at": held_at_iso,
        "id": record_id,
        "f": fingerprint,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _b64encode(raw)


def decode_cursor(token: str, fingerprint: str) -> Tuple[str, int]:
    """解析并校验游标，返回 ``(held_at_iso, id)``。

    篡改、版本不符或筛选指纹不一致时抛出 :class:`CursorError`，
    调用方据此返回 ``invalid_cursor`` / ``stale_cursor`` 错误。
    """
    try:
        raw = _b64decode(token)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # 任何非预期输入都按非法游标处理
        raise CursorError("invalid_cursor", "分页游标无法解析，请从第一页重新开始浏览") from exc

    if not isinstance(payload, dict):
        raise CursorError("invalid_cursor", "分页游标格式不正确")
    if payload.get("v") != CURSOR_VERSION:
        raise CursorError("invalid_cursor", "分页游标版本不支持，请重新获取第一页")
    held_at_iso = payload.get("held_at")
    record_id = payload.get("id")
    if not isinstance(held_at_iso, str) or not isinstance(record_id, int) or isinstance(record_id, bool):
        raise CursorError("invalid_cursor", "分页游标内容不完整")

    saved_fingerprint = payload.get("f")
    if not isinstance(saved_fingerprint, str) or saved_fingerprint != fingerprint:
        raise CursorError(
            "stale_cursor",
            "筛选条件已变化，原分页游标失效，请清空游标后从第一页重新查询",
        )
    return held_at_iso, record_id
