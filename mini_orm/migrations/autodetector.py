"""The autodetector: it finds the changes between two schema states.

makemigrations uses it. The old state comes from the migration files. The
new state comes from the models. The result is the list of operations that
changes the old state into the new state.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

from ..exceptions import MigrationError
from ..graph import sort_by_dependencies
from ..schema import Column
from .operations import AddColumn, AlterColumn, CreateTable, DropColumn, DropTable, Operation
from .state import SchemaState

if TYPE_CHECKING:
    from ..fields import Field
    from ..models import Model

FieldsByColumn = dict[tuple[str, str], "Field"]


class Changes(NamedTuple):
    operations: list[Operation]
    # Information for the developer, for example a possible rename.
    notes: list[str]


def detect_changes(old: SchemaState, new: SchemaState, models: Iterable[type[Model]]) -> Changes:
    """Return the operations that change the old state into the new state.

    The order of the operations is safe for foreign keys:

    1. Create the new tables. A table comes after the tables that it refers to.
    2. Add, alter and drop the columns of the tables that stay.
    3. Drop the old tables. A table comes before the tables that it refers to.
    """
    fields = _fields_by_column(models)
    operations: list[Operation] = []
    notes: list[str] = []
    created = [table for table in new.tables if table not in old.tables]
    dropped = [table for table in old.tables if table not in new.tables]

    for table in _sort_by_references(created, new):
        operations.append(CreateTable(table, list(new.tables[table])))

    for table, new_columns in new.tables.items():
        if table in old.tables:
            table_operations, table_notes = _column_changes(table, old.tables[table], new_columns, fields)
            operations += table_operations
            notes += table_notes

    for table in reversed(_sort_by_references(dropped, old)):
        operations.append(DropTable(table))

    for dropped_table in dropped:
        for created_table in created:
            if old.tables[dropped_table] == new.tables[created_table]:
                notes.append(
                    f"The table {dropped_table} was dropped and the table {created_table} was "
                    "created with the same columns. If it is a rename, replace the CreateTable "
                    f'and the DropTable with RenameTable("{dropped_table}", "{created_table}") '
                    "to keep the rows."
                )
    return Changes(operations, notes)


def _column_changes(
    table: str,
    old_columns: list[Column],
    new_columns: list[Column],
    fields: FieldsByColumn,
) -> tuple[list[Operation], list[str]]:
    old_by_name = {column.name: column for column in old_columns}
    new_names = {column.name for column in new_columns}
    added = [column for column in new_columns if column.name not in old_by_name]
    removed = [column for column in old_columns if column.name not in new_names]
    rename = _possible_rename(added, removed)

    operations: list[Operation] = []
    for column in new_columns:
        old_column = old_by_name.get(column.name)
        if old_column is None and rename is not None:
            # The developer replaces this operation with RenameColumn (see the
            # note), so the rows need no value from a default.
            operations.append(AddColumn(table, column))
        elif old_column is None:
            operations.append(AddColumn(table, column, _value_for_existing_rows(table, column, fields)))
        elif column != old_column:
            default = None
            if old_column.null and not column.null:
                default = _constant_default(fields.get((table, column.name)))
            operations.append(AlterColumn(table, column, default))
    for column in removed:
        operations.append(DropColumn(table, column.name))

    notes = []
    if rename is not None:
        old_name, new_name = rename
        notes.append(
            f"The column {table}.{old_name} was dropped and the column {table}.{new_name} was "
            "added with the same definition. If it is a rename, replace the AddColumn and the "
            f'DropColumn with RenameColumn("{table}", "{old_name}", "{new_name}") to keep the values.'
        )
    return operations, notes


def _possible_rename(added: list[Column], removed: list[Column]) -> tuple[str, str] | None:
    """Return (old name, new name) if one dropped column and one new column look like a rename."""
    if len(added) == 1 and len(removed) == 1:
        new_column, old_column = added[0], removed[0]
        if dataclasses.replace(old_column, name=new_column.name) == new_column:
            return old_column.name, new_column.name
    return None


def _value_for_existing_rows(table: str, column: Column, fields: FieldsByColumn) -> Any:
    """Return the value that the rows of a table get for a new column."""
    if column.null:
        return None
    default = _constant_default(fields.get((table, column.name)))
    if default is None:
        raise MigrationError(
            f"The new column {table}.{column.name} is NOT NULL and has no constant default. "
            "The rows that exist need a value for it. Give the field a default or null=True. "
            "Or write the migration by hand with AddColumn(..., default=value)."
        )
    return default


def _constant_default(field: Field | None) -> Any:
    """Return the database value of the default of a field, if it is a constant."""
    if field is None or field.default is None or callable(field.default):
        return None
    return field.to_db(field.default)


def _fields_by_column(models: Iterable[type[Model]]) -> FieldsByColumn:
    return {
        (model._meta.table_name, field.column): field
        for model in models
        for field in model._meta.fields.values()
    }


def _sort_by_references(tables: list[str], state: SchemaState) -> list[str]:
    def referenced_tables(table: str) -> list[str]:
        return [column.references[0] for column in state.tables[table] if column.references is not None]

    return sort_by_dependencies(tables, referenced_tables)
