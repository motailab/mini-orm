"""The schema state: the tables and the columns, without a database.

The migrations do not read the database to find the current schema. They
replay the operations of the migration files on an empty state. Django
does the same. So makemigrations works without a database, and the result
does not depend on the database.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..exceptions import MigrationError
from ..schema import Column, table_columns

if TYPE_CHECKING:
    from ..models import Model
    from .migration import Migration


class SchemaState:
    """Table name -> the columns of the table, in column order."""

    def __init__(self, tables: dict[str, list[Column]] | None = None) -> None:
        self.tables: dict[str, list[Column]] = tables if tables is not None else {}

    @classmethod
    def from_models(cls, models: Iterable[type[Model]]) -> SchemaState:
        """Return the state that the models describe."""
        return cls({model._meta.table_name: table_columns(model) for model in models})

    def copy(self) -> SchemaState:
        # A Column is immutable, so a new list for each table is a full copy.
        return SchemaState({table: list(columns) for table, columns in self.tables.items()})

    def columns(self, table: str) -> list[Column]:
        """Return the list of the columns of a table. Changes to the list change the state."""
        if table not in self.tables:
            raise MigrationError(f"The migration state has no table {table!r}.")
        return self.tables[table]

    def column(self, table: str, name: str) -> Column:
        for column in self.columns(table):
            if column.name == name:
                return column
        raise MigrationError(f"The migration state has no column {table}.{name}.")


def state_after(migrations: Iterable[Migration]) -> SchemaState:
    """Replay the operations of the migrations on an empty state."""
    state = SchemaState()
    for migration in migrations:
        for operation in migration.operations:
            operation.apply_to_state(state)
    return state
