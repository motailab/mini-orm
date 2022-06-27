"""Migration operations. Each operation is one change of the schema.

An operation has these methods:

* apply_to_state(state): change the in-memory schema state
* forwards(state, editor): return the SQL statements that make the change
* backwards(state, editor): return the SQL statements that undo the change
* describe(): return a short text for the command output
* name_fragment(): return a short text for the name of a migration file

In forwards() and backwards(), ``state`` is always the state BEFORE the
operation, in the forward direction. backwards() needs it too: to undo a
DropTable, the operation must know the columns of the table.

The operation knows WHAT changes. The schema editor of the database knows
HOW to do it (see schema_editor.py). So one migration file works with
SQLite, PostgreSQL and MySQL.

Each operation is a dataclass. The writer reads the fields of the
dataclass to write the operation into a migration file, and two operations
with the same values are equal.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from ..exceptions import IrreversibleError, MigrationError
from ..schema import Column
from .schema_editor import SchemaEditor, Statement
from .state import SchemaState


class Operation:
    """The base class of all migration operations."""

    def apply_to_state(self, state: SchemaState) -> None:
        raise NotImplementedError

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        raise NotImplementedError

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def name_fragment(self) -> str:
        raise NotImplementedError


@dataclass
class CreateTable(Operation):
    """Create a table."""

    table: str
    columns: list[Column]

    def apply_to_state(self, state: SchemaState) -> None:
        if self.table in state.tables:
            raise MigrationError(f"The table {self.table!r} exists already.")
        state.tables[self.table] = list(self.columns)

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        return editor.create_table(self.table, self.columns)

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        return editor.drop_table(self.table)

    def describe(self) -> str:
        return f"Create table {self.table}"

    def name_fragment(self) -> str:
        return f"create_{self.table}"


@dataclass
class DropTable(Operation):
    """Drop a table. Its rows are lost."""

    table: str

    def apply_to_state(self, state: SchemaState) -> None:
        state.columns(self.table)  # Raise an error if the table does not exist.
        del state.tables[self.table]

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        state.columns(self.table)
        return editor.drop_table(self.table)

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        # Make the table again from the state before the drop. The rows do not come back.
        return editor.create_table(self.table, state.columns(self.table))

    def describe(self) -> str:
        return f"Drop table {self.table}"

    def name_fragment(self) -> str:
        return f"drop_{self.table}"


@dataclass
class AddColumn(Operation):
    """Add a column to a table.

    ``default`` is a value for the rows that exist already, for example 0.
    The column itself gets no default. A NOT NULL column needs it if the
    table has rows.
    """

    table: str
    column: Column
    default: Any = None

    def apply_to_state(self, state: SchemaState) -> None:
        columns = state.columns(self.table)
        if any(existing.name == self.column.name for existing in columns):
            raise MigrationError(f"The column {self.table}.{self.column.name} exists already.")
        columns.append(self.column)

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        old_columns = state.columns(self.table)
        new_columns = [*old_columns, self.column]
        return editor.change_table(self.table, old_columns, new_columns, {self.column.name: self.default})

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        old_columns = state.columns(self.table)
        return editor.change_table(self.table, [*old_columns, self.column], old_columns)

    def describe(self) -> str:
        return f"Add column {self.column.name} to {self.table}"

    def name_fragment(self) -> str:
        return f"add_{self.table}_{self.column.name}"


@dataclass
class DropColumn(Operation):
    """Remove a column from a table. Its values are lost."""

    table: str
    name: str

    def apply_to_state(self, state: SchemaState) -> None:
        state.columns(self.table).remove(state.column(self.table, self.name))

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        state.column(self.table, self.name)  # Raise an error if the column does not exist.
        old_columns = state.columns(self.table)
        new_columns = [column for column in old_columns if column.name != self.name]
        return editor.change_table(self.table, old_columns, new_columns)

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        # The column comes back with NULL values. A NOT NULL column fails if the table has rows.
        old_columns = state.columns(self.table)
        current_columns = [column for column in old_columns if column.name != self.name]
        return editor.change_table(self.table, current_columns, old_columns)

    def describe(self) -> str:
        return f"Drop column {self.name} from {self.table}"

    def name_fragment(self) -> str:
        return f"drop_{self.table}_{self.name}"


@dataclass
class AlterColumn(Operation):
    """Change the definition of a column: the type, NULL, UNIQUE or the foreign key.

    ``column`` is the new definition. Its name tells which column changes.
    ``default`` is a value for the NULL values, for a change from NULL to
    NOT NULL.
    """

    table: str
    column: Column
    default: Any = None

    def apply_to_state(self, state: SchemaState) -> None:
        columns = state.columns(self.table)
        old_column = state.column(self.table, self.column.name)
        columns[columns.index(old_column)] = self.column

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        state.column(self.table, self.column.name)
        old_columns = state.columns(self.table)
        new_columns = _replace_column(old_columns, self.column)
        return editor.change_table(self.table, old_columns, new_columns, {self.column.name: self.default})

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        old_columns = state.columns(self.table)
        current_columns = _replace_column(old_columns, self.column)
        return editor.change_table(self.table, current_columns, old_columns)

    def describe(self) -> str:
        return f"Alter column {self.column.name} of {self.table}"

    def name_fragment(self) -> str:
        return f"alter_{self.table}_{self.column.name}"


@dataclass
class RenameColumn(Operation):
    """Rename a column. The values stay."""

    table: str
    old_name: str
    new_name: str

    def apply_to_state(self, state: SchemaState) -> None:
        columns = state.columns(self.table)
        column = state.column(self.table, self.old_name)
        columns[columns.index(column)] = dataclasses.replace(column, name=self.new_name)
        # The database also changes the foreign keys of other tables that refer to the column.
        _replace_references(state, (self.table, self.old_name), (self.table, self.new_name))

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        state.column(self.table, self.old_name)
        return editor.rename_column(self.table, state.columns(self.table), self.old_name, self.new_name)

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        renamed = [
            dataclasses.replace(column, name=self.new_name) if column.name == self.old_name else column
            for column in state.columns(self.table)
        ]
        return editor.rename_column(self.table, renamed, self.new_name, self.old_name)

    def describe(self) -> str:
        return f"Rename column {self.old_name} of {self.table} to {self.new_name}"

    def name_fragment(self) -> str:
        return f"rename_{self.table}_{self.old_name}"


@dataclass
class RenameTable(Operation):
    """Rename a table. The rows stay."""

    old_name: str
    new_name: str

    def apply_to_state(self, state: SchemaState) -> None:
        columns = state.columns(self.old_name)
        if self.new_name in state.tables:
            raise MigrationError(f"The table {self.new_name!r} exists already.")
        del state.tables[self.old_name]
        state.tables[self.new_name] = columns
        # The database also changes the foreign keys of other tables that refer to the table.
        for table_columns in state.tables.values():
            for index, column in enumerate(table_columns):
                if column.references is not None and column.references[0] == self.old_name:
                    new_reference = (self.new_name, column.references[1])
                    table_columns[index] = dataclasses.replace(column, references=new_reference)

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        return editor.rename_table(self.old_name, self.new_name, state.columns(self.old_name))

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        return editor.rename_table(self.new_name, self.old_name, state.columns(self.old_name))

    def describe(self) -> str:
        return f"Rename table {self.old_name} to {self.new_name}"

    def name_fragment(self) -> str:
        return f"rename_{self.old_name}"


@dataclass
class RunSQL(Operation):
    """Run one SQL statement of your own, for example to change data.

    Write the statement for your database. The statement does not change
    the migration state. ``reverse_sql`` undoes the statement. Without it,
    the migration cannot be unapplied.
    """

    sql: str
    reverse_sql: str | None = None

    def apply_to_state(self, state: SchemaState) -> None:
        pass

    def forwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        return [(self.sql, [])]

    def backwards(self, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
        if self.reverse_sql is None:
            raise IrreversibleError(f"RunSQL has no reverse_sql, so it cannot be undone: {self.sql}")
        return [(self.reverse_sql, [])]

    def describe(self) -> str:
        return f"Run SQL: {self.sql}"

    def name_fragment(self) -> str:
        return "run_sql"


def _replace_column(columns: list[Column], new_column: Column) -> list[Column]:
    return [new_column if column.name == new_column.name else column for column in columns]


def _replace_references(state: SchemaState, old: tuple[str, str], new: tuple[str, str]) -> None:
    for columns in state.tables.values():
        for index, column in enumerate(columns):
            if column.references == old:
                columns[index] = dataclasses.replace(column, references=new)
