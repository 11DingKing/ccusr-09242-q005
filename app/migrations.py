"""轻量级、幂等的 SQLite 结构演进。

项目未引入 Alembic；在 ``Base.metadata.create_all`` 之前对已有数据库做
增量列补齐，避免旧库缺列导致接口报错。每次升级在此追加新步骤即可。
"""

from sqlalchemy import inspect, text

from .database import engine


def _column_exists(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def run_migrations() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())

        if "negotiation_records" in tables and not _column_exists(
            inspector, "negotiation_records", "recorded_at"
        ):
            conn.execute(
                text(
                    "ALTER TABLE negotiation_records "
                    "ADD COLUMN recorded_at DATETIME"
                )
            )
            # 历史数据没有单独的录入时间，按“按时录入”处理（以业务发生时间回填），
            # 避免存量记录被误判为迟到补录。
            conn.execute(
                text(
                    "UPDATE negotiation_records "
                    "SET recorded_at = COALESCE(held_at, created_at, CURRENT_TIMESTAMP) "
                    "WHERE recorded_at IS NULL"
                )
            )
