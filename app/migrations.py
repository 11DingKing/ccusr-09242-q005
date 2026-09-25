"""轻量级启动期结构补齐。

项目使用 SQLite 且没有引入迁移框架，``Base.metadata.create_all`` 只能建新表、
不会为已存在的表增加列。这里对 ``negotiation_records.recorded_at``
（录入时间）做幂等补齐，并把历史数据回填为 created_at，
保证旧库升级后时间线与迟到补录判断仍然可用。
"""

from sqlalchemy import text


def ensure_negotiation_recorded_at(engine) -> None:
    with engine.begin() as conn:
        columns = conn.execute(
            text("PRAGMA table_info(negotiation_records)")
        ).fetchall()
        if not columns:
            return
        existing = {row[1] for row in columns}
        if "recorded_at" not in existing:
            conn.execute(
                text(
                    "ALTER TABLE negotiation_records "
                    "ADD COLUMN recorded_at DATETIME"
                )
            )
        conn.execute(
            text(
                "UPDATE negotiation_records "
                "SET recorded_at = created_at WHERE recorded_at IS NULL"
            )
        )


def run_startup_migrations(engine) -> None:
    ensure_negotiation_recorded_at(engine)
