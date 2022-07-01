"""The migration recorder: the table that lists the applied migrations."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from ..schema import Column

if TYPE_CHECKING:
    from ..database import Database

TABLE = "mini_orm_migrations"
COLUMNS = [
    Column("name", "string", max_length=255, primary_key=True, null=False),
    Column("applied_at", "string", max_length=32, null=False),
]


class MigrationRecorder:
    """Read and write the table of the applied migrations."""

    def __init__(self, database: Database) -> None:
        self.database = database
        dialect = database.dialect
        self.table = dialect.quote_name(TABLE)
        self.name = dialect.quote_name("name")
        self.applied_at = dialect.quote_name("applied_at")
        self.placeholder = dialect.placeholder

    def ensure_table(self) -> None:
        self.database.execute(self.database.dialect.create_table_sql(TABLE, COLUMNS))

    def applied_names(self) -> list[str]:
        """Return the names of the applied migrations, in name order."""
        self.ensure_table()
        rows = self.database.execute(f"SELECT {self.name} FROM {self.table} ORDER BY {self.name}").fetchall()
        return [row[0] for row in rows]

    def record_applied(self, name: str) -> None:
        applied_at = datetime.datetime.now().isoformat(timespec="seconds")
        self.database.execute(
            f"INSERT INTO {self.table} ({self.name}, {self.applied_at}) "
            f"VALUES ({self.placeholder}, {self.placeholder})",
            [name, applied_at],
        )

    def record_unapplied(self, name: str) -> None:
        self.database.execute(f"DELETE FROM {self.table} WHERE {self.name} = {self.placeholder}", [name])
