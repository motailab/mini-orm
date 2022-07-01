"""Schema editors: the SQL of each schema change, for each database.

A migration operation knows WHAT changes, for example "add the column isbn".
A schema editor knows HOW the database does it:

* SQLite cannot change a column in place. Its editor uses the table remake:
  make a new table, copy the rows, drop the old table, rename the new table.
* PostgreSQL and MySQL can change a column in place with ALTER TABLE. Their
  editors compare the old columns with the new columns and write one
  statement for each difference.

Each method returns a list of statements. A statement is a pair (SQL text,
parameters), as in the rest of the ORM.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..exceptions import MigrationError

if TYPE_CHECKING:
    from ..dialects import Dialect
    from ..schema import Column

Statement = tuple[str, list[Any]]


class SchemaEditor:
    """The base class. It has the statements that are the same in each database."""

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    # -- Before and after a migration ----------------------------------------

    def before_migration(self) -> list[str]:
        """Statements that run before the BEGIN of each migration."""
        return []

    def after_migration(self) -> list[str]:
        """Statements that run after the end of each migration."""
        return []

    def check_sql(self) -> str | None:
        """A query that runs before the COMMIT. Each row that it returns is a problem."""
        return None

    # -- Tables ---------------------------------------------------------------

    def create_table(self, table: str, columns: Sequence[Column]) -> list[Statement]:
        return [(self.dialect.create_table_sql(table, columns, if_not_exists=False), [])]

    def drop_table(self, table: str) -> list[Statement]:
        return [(f"DROP TABLE {self.dialect.quote_name(table)}", [])]

    def rename_table(self, old_name: str, new_name: str, columns: Sequence[Column]) -> list[Statement]:
        quote = self.dialect.quote_name
        return [(f"ALTER TABLE {quote(old_name)} RENAME TO {quote(new_name)}", [])]

    # -- Columns --------------------------------------------------------------

    def change_table(
        self,
        table: str,
        old_columns: Sequence[Column],
        new_columns: Sequence[Column],
        defaults: dict[str, Any] | None = None,
    ) -> list[Statement]:
        """Change the columns of a table from old_columns to new_columns.

        ``defaults`` maps a column name to a value. A new column gets the
        value in each row. A changed column gets the value where it is NULL.
        """
        raise NotImplementedError

    def rename_column(
        self, table: str, columns: Sequence[Column], old_name: str, new_name: str
    ) -> list[Statement]:
        quote = self.dialect.quote_name
        return [(f"ALTER TABLE {quote(table)} RENAME COLUMN {quote(old_name)} TO {quote(new_name)}", [])]


class SQLiteSchemaEditor(SchemaEditor):
    """SQLite: each change of a column is a table remake."""

    def before_migration(self) -> list[str]:
        # PRAGMA foreign_keys has no effect inside a transaction, so it comes
        # before BEGIN. Without it, the DROP TABLE of the remake deletes the
        # rows of the table, and ON DELETE CASCADE deletes the child rows too.
        return ["PRAGMA foreign_keys = OFF"]

    def after_migration(self) -> list[str]:
        return ["PRAGMA foreign_keys = ON"]

    def check_sql(self) -> str | None:
        # The checks were off, so look for rows that break a foreign key now.
        return "PRAGMA foreign_key_check"

    def change_table(
        self,
        table: str,
        old_columns: Sequence[Column],
        new_columns: Sequence[Column],
        defaults: dict[str, Any] | None = None,
    ) -> list[Statement]:
        """The SQLite "table remake" procedure.

        SQLite cannot change the type or the constraints of a column in place.
        So the procedure makes a new table, copies the rows, drops the old
        table and gives the new table the old name.

        The copy follows three rules. A column that exists in the old table
        gets the old value. A new column gets its default, or NULL. A changed
        column with a default gets COALESCE(column, default).
        """
        defaults = defaults or {}
        quote = self.dialect.quote_name
        placeholder = self.dialect.placeholder
        old_names = {column.name for column in old_columns}
        temporary = f"new__{table}"
        targets: list[str] = []
        sources: list[str] = []
        params: list[Any] = []
        for column in new_columns:
            name = quote(column.name)
            default = defaults.get(column.name)
            targets.append(name)
            if column.name not in old_names and default is None:
                sources.append("NULL")
            elif column.name not in old_names:
                sources.append(placeholder)
                params.append(default)
            elif default is None:
                sources.append(name)
            else:
                sources.append(f"COALESCE({name}, {placeholder})")
                params.append(default)
        copy_rows = (
            f"INSERT INTO {quote(temporary)} ({', '.join(targets)}) "
            f"SELECT {', '.join(sources)} FROM {quote(table)}"
        )
        return [
            (self.dialect.create_table_sql(temporary, new_columns, if_not_exists=False), []),
            (copy_rows, params),
            (f"DROP TABLE {quote(table)}", []),
            (f"ALTER TABLE {quote(temporary)} RENAME TO {quote(table)}", []),
        ]


class AlterTableSchemaEditor(SchemaEditor):
    """The editor of a database that can change a column in place: PostgreSQL and MySQL.

    change_table() compares the old columns with the new columns. A dropped
    column, a new column and a changed column each get their statements.
    The UNIQUE and FOREIGN KEY constraints have names (see
    Dialect.table_constraints), so the editor can drop and add them.

    The subclasses give the statements that are different in the two
    databases: a change of the type or of NULL, the drop of a constraint,
    and the rename of a constraint.
    """

    def change_table(
        self,
        table: str,
        old_columns: Sequence[Column],
        new_columns: Sequence[Column],
        defaults: dict[str, Any] | None = None,
    ) -> list[Statement]:
        defaults = defaults or {}
        old_by_name = {column.name: column for column in old_columns}
        new_names = {column.name for column in new_columns}
        statements: list[Statement] = []
        for column in old_columns:
            if column.name not in new_names:
                statements += self._drop_column(table, column)
        for column in new_columns:
            old_column = old_by_name.get(column.name)
            if old_column is None:
                statements += self._add_column(table, column, defaults.get(column.name))
            elif old_column != column:
                statements += self._alter_column(table, old_column, column, defaults.get(column.name))
        return statements

    def _add_column(self, table: str, column: Column, default: Any) -> list[Statement]:
        """Add the column without constraints, fill the rows, then add the constraints.

        A NOT NULL constraint on a table with rows needs a value in each row
        first. So the column starts as NULL, and _alter_column() adds the
        NOT NULL, UNIQUE and FOREIGN KEY constraints at the end.
        """
        bare = dataclasses.replace(column, null=True, unique=False, references=None, on_delete=None)
        statements = [self._alter(table, f"ADD COLUMN {self.dialect.column_definition(bare)}")]
        if default is not None:
            statements.append(self._update(table, column, default, only_null=False))
        return statements + self._alter_column(table, bare, column, None)

    def _drop_column(self, table: str, column: Column) -> list[Statement]:
        # Drop the constraints of the column first. MySQL refuses to drop a
        # column that a FOREIGN KEY constraint uses.
        bare = dataclasses.replace(column, unique=False, references=None, on_delete=None)
        statements = self._alter_column(table, column, bare, None)
        statements.append(self._alter(table, f"DROP COLUMN {self.dialect.quote_name(column.name)}"))
        return statements

    def _alter_column(self, table: str, old: Column, new: Column, default: Any) -> list[Statement]:
        if old.primary_key != new.primary_key:
            raise MigrationError(f"mini_orm cannot add or remove the primary key of {table}.{new.name}.")
        old_key = (old.references, old.on_delete)
        new_key = (new.references, new.on_delete)
        statements: list[Statement] = []
        if old.references is not None and old_key != new_key:
            statements.append(self._drop_foreign_key(table, old.name))
        if old.unique and not new.unique:
            statements.append(self._drop_unique(table, old.name))
        if default is not None and not new.null:
            statements.append(self._update(table, new, default, only_null=True))
        if self.dialect.column_type(old) != self.dialect.column_type(new) or old.null != new.null:
            statements += self._change_column(table, old, new)
        if new.unique and not old.unique:
            name = self.dialect.quote_name(self.dialect.unique_name(table, new.name))
            statements.append(
                self._alter(table, f"ADD CONSTRAINT {name} UNIQUE ({self.dialect.quote_name(new.name)})")
            )
        if new.references is not None and old_key != new_key:
            statements.append(self._add_foreign_key(table, new))
        return statements

    # -- Helpers for the subclasses -------------------------------------------

    def _alter(self, table: str, action: str) -> Statement:
        return (f"ALTER TABLE {self.dialect.quote_name(table)} {action}", [])

    def _update(self, table: str, column: Column, value: Any, *, only_null: bool) -> Statement:
        quote = self.dialect.quote_name
        sql = f"UPDATE {quote(table)} SET {quote(column.name)} = {self.dialect.placeholder}"
        if only_null:
            sql += f" WHERE {quote(column.name)} IS NULL"
        return (sql, [value])

    def _add_foreign_key(self, table: str, column: Column) -> Statement:
        name = self.dialect.quote_name(self.dialect.foreign_key_name(table, column.name))
        return self._alter(table, f"ADD CONSTRAINT {name} {self.dialect.foreign_key_sql(column)}")

    # -- Statements that are different in PostgreSQL and MySQL ----------------

    def _change_column(self, table: str, old: Column, new: Column) -> list[Statement]:
        """Change the type or the NULL setting of a column."""
        raise NotImplementedError

    def _drop_unique(self, table: str, column_name: str) -> Statement:
        raise NotImplementedError

    def _drop_foreign_key(self, table: str, column_name: str) -> Statement:
        raise NotImplementedError


class PostgreSQLSchemaEditor(AlterTableSchemaEditor):
    def _change_column(self, table: str, old: Column, new: Column) -> list[Statement]:
        column = self.dialect.quote_name(new.name)
        statements = []
        new_type = self.dialect.column_type(new)
        if self.dialect.column_type(old) != new_type:
            # USING tells PostgreSQL how to convert the values that exist.
            statements.append(
                self._alter(table, f"ALTER COLUMN {column} TYPE {new_type} USING {column}::{new_type}")
            )
        if old.null != new.null:
            action = "DROP NOT NULL" if new.null else "SET NOT NULL"
            statements.append(self._alter(table, f"ALTER COLUMN {column} {action}"))
        return statements

    def _drop_unique(self, table: str, column_name: str) -> Statement:
        name = self.dialect.quote_name(self.dialect.unique_name(table, column_name))
        return self._alter(table, f"DROP CONSTRAINT {name}")

    def _drop_foreign_key(self, table: str, column_name: str) -> Statement:
        name = self.dialect.quote_name(self.dialect.foreign_key_name(table, column_name))
        return self._alter(table, f"DROP CONSTRAINT {name}")

    def rename_column(
        self, table: str, columns: Sequence[Column], old_name: str, new_name: str
    ) -> list[Statement]:
        statements = super().rename_column(table, columns, old_name, new_name)
        column = _find(columns, old_name)
        # The names of the constraints contain the column name. Keep them correct.
        statements += self._rename_constraints(table, table, column, new_name)
        return statements

    def rename_table(self, old_name: str, new_name: str, columns: Sequence[Column]) -> list[Statement]:
        statements = super().rename_table(old_name, new_name, columns)
        # PostgreSQL keeps the foreign keys of other tables that refer to this
        # table. The names of the constraints of this table contain the table name.
        for column in columns:
            statements += self._rename_constraints(old_name, new_name, column, column.name)
        return statements

    def _rename_constraints(
        self, old_table: str, new_table: str, column: Column, new_column: str
    ) -> list[Statement]:
        statements = []
        if column.unique and not column.primary_key:
            statements.append(
                self._rename_constraint(
                    new_table,
                    self.dialect.unique_name(old_table, column.name),
                    self.dialect.unique_name(new_table, new_column),
                )
            )
        if column.references is not None:
            statements.append(
                self._rename_constraint(
                    new_table,
                    self.dialect.foreign_key_name(old_table, column.name),
                    self.dialect.foreign_key_name(new_table, new_column),
                )
            )
        return statements

    def _rename_constraint(self, table: str, old_name: str, new_name: str) -> Statement:
        quote = self.dialect.quote_name
        return self._alter(table, f"RENAME CONSTRAINT {quote(old_name)} TO {quote(new_name)}")


class MySQLSchemaEditor(AlterTableSchemaEditor):
    def _change_column(self, table: str, old: Column, new: Column) -> list[Statement]:
        # MODIFY COLUMN gives the full new definition: the type and NULL together.
        if new.primary_key:
            raise MigrationError(
                f"mini_orm cannot change the primary key column {table}.{new.name} on MySQL."
            )
        return [self._alter(table, f"MODIFY COLUMN {self.dialect.column_definition(new)}")]

    def _drop_unique(self, table: str, column_name: str) -> Statement:
        # In MySQL, a UNIQUE constraint is an index with the same name.
        name = self.dialect.quote_name(self.dialect.unique_name(table, column_name))
        return self._alter(table, f"DROP INDEX {name}")

    def _drop_foreign_key(self, table: str, column_name: str) -> Statement:
        name = self.dialect.quote_name(self.dialect.foreign_key_name(table, column_name))
        return self._alter(table, f"DROP FOREIGN KEY {name}")

    def rename_column(
        self, table: str, columns: Sequence[Column], old_name: str, new_name: str
    ) -> list[Statement]:
        column = _find(columns, old_name)
        renamed = dataclasses.replace(column, name=new_name)
        # MySQL cannot rename a foreign key constraint. So drop it first, and
        # add it again with the new name after the rename of the column.
        statements = []
        if column.references is not None:
            statements.append(self._drop_foreign_key(table, old_name))
        statements += super().rename_column(table, columns, old_name, new_name)
        if column.unique and not column.primary_key:
            statements.append(
                self._rename_index(
                    table,
                    self.dialect.unique_name(table, old_name),
                    self.dialect.unique_name(table, new_name),
                )
            )
        if column.references is not None:
            statements.append(self._add_foreign_key(table, renamed))
        return statements

    def rename_table(self, old_name: str, new_name: str, columns: Sequence[Column]) -> list[Statement]:
        statements = []
        for column in columns:
            if column.references is not None:
                statements.append(self._drop_foreign_key(old_name, column.name))
        statements += super().rename_table(old_name, new_name, columns)
        for column in columns:
            if column.unique and not column.primary_key:
                old_index = self.dialect.unique_name(old_name, column.name)
                statements.append(
                    self._rename_index(new_name, old_index, self.dialect.unique_name(new_name, column.name))
                )
            if column.references is not None:
                statements.append(self._add_foreign_key(new_name, column))
        return statements

    def _rename_index(self, table: str, old_name: str, new_name: str) -> Statement:
        quote = self.dialect.quote_name
        return self._alter(table, f"RENAME INDEX {quote(old_name)} TO {quote(new_name)}")


def schema_editor_for(dialect: Dialect) -> SchemaEditor:
    """Return the schema editor of a dialect."""
    editors: dict[str, type[SchemaEditor]] = {
        "sqlite": SQLiteSchemaEditor,
        "postgresql": PostgreSQLSchemaEditor,
        "mysql": MySQLSchemaEditor,
    }
    return editors[dialect.name](dialect)


def _find(columns: Sequence[Column], name: str) -> Column:
    for column in columns:
        if column.name == name:
            return column
    raise MigrationError(f"The column {name!r} does not exist.")
