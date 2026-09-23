from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration


@dataclass(frozen=True)
class Migration:
    version: str
    description: str


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(version="0002", description="Offline action pack import records"),
]

_INCIDENT_CLIENT_REF_INDEX = "uq_incident_expedition_client_ref"


def _apply_0002(session: Session) -> None:
    """离线行动包需要事件 client_ref；为旧库补列与唯一索引。"""

    columns = {
        row[1]
        for row in session.execute(text("PRAGMA table_info(emergency_incidents)")).all()
    }
    if "client_ref" not in columns:
        session.execute(
            text("ALTER TABLE emergency_incidents ADD COLUMN client_ref VARCHAR(80)")
        )
    indexes = {
        row[1]
        for row in session.execute(text("PRAGMA index_list(emergency_incidents)")).all()
    }
    if _INCIDENT_CLIENT_REF_INDEX not in indexes:
        # SQLite 唯一索引把多个 NULL 视为互不相等，在线事件 client_ref=NULL 不受影响。
        session.execute(
            text(
                f"CREATE UNIQUE INDEX {_INCIDENT_CLIENT_REF_INDEX} "
                "ON emergency_incidents (expedition_id, client_ref)"
            )
        )


def initialize_database(database: Database) -> list[str]:
    database.create_schema()
    applied: list[str] = []
    with database.session() as session:
        known = {
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        }
        for migration in MIGRATIONS:
            if migration.version in known:
                continue
            if migration.version == "0002":
                _apply_0002(session)
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
            applied.append(migration.version)
    return applied


def migration_status(database: Database) -> dict[str, object]:
    inspector = inspect(database.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return {
            "initialized": False,
            "applied": [],
            "pending": [item.version for item in MIGRATIONS],
        }
    with database.session() as session:
        applied = [
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        ]
    pending = [item.version for item in MIGRATIONS if item.version not in set(applied)]
    return {"initialized": True, "applied": applied, "pending": pending}


def assert_database_integrity(database: Database) -> dict[str, object]:
    with database.engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "healthy": integrity == "ok" and not foreign_key_rows,
    }
