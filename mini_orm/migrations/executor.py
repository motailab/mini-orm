"""The migration executor: it applies and unapplies migrations (the migrate command)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..exceptions import MigrationError
from .recorder import MigrationRecorder
from .schema_editor import SchemaEditor, Statement, schema_editor_for
from .state import SchemaState, state_after

if TYPE_CHECKING:
    from ..database import Database
    from .migration import Migration


class MigrationExecutor:
    """Apply or unapply the migrations of a chain on one database."""

    def __init__(self, database: Database, migrations: Sequence[Migration]) -> None:
        self.database = database
        self.migrations = list(migrations)
        self.recorder = MigrationRecorder(database)
        self.editor = schema_editor_for(database.dialect)

    def applied_migrations(self) -> list[Migration]:
        """Return the applied migrations. They must be the first migrations of the chain."""
        names = self.recorder.applied_names()
        expected = [migration.name for migration in self.migrations[: len(names)]]
        if names != expected:
            raise MigrationError(
                f"The database has the applied migrations {names}, but the migration files "
                f"start with {expected}. The migration history is not consistent."
            )
        return self.migrations[: len(names)]

    def migrate(self, target: str | None = None) -> list[str]:
        """Apply or unapply migrations, so that the target is the last applied migration.

        * target None: apply all migrations
        * target "zero": unapply all migrations
        * target a name or a unique name prefix, for example "0002": apply or
          unapply migrations until that migration is the last applied one

        Return one message for each applied or unapplied migration.
        """
        if self.database.in_transaction:
            raise MigrationError("The database connection has an open transaction. Close the session first.")
        applied = self.applied_migrations()
        if target is None:
            target_index = len(self.migrations) - 1
        elif target == "zero":
            target_index = -1
        else:
            target_index = find_migration(self.migrations, target)

        messages = []
        if target_index >= len(applied) - 1:
            state = state_after(applied)
            for migration in self.migrations[len(applied) : target_index + 1]:
                self._run(forwards_statements(migration, state, self.editor), migration.name, applied=True)
                messages.append(f"Applied {migration.name}")
        else:
            for migration in reversed(applied[target_index + 1 :]):
                index = self.migrations.index(migration)
                self._run(
                    backwards_statements(self.migrations, index, self.editor), migration.name, applied=False
                )
                messages.append(f"Unapplied {migration.name}")
        return messages

    def _run(self, statements: list[Statement], name: str, *, applied: bool) -> None:
        """Run the statements of one migration and record the result, in one transaction.

        The schema editor can add statements before and after the transaction,
        for example to turn the foreign key checks of SQLite off and on.
        """
        for sql in self.editor.before_migration():
            self.database.execute(sql)
        try:
            self._run_in_transaction(statements, name, applied=applied)
        finally:
            for sql in self.editor.after_migration():
                self.database.execute(sql)

    def _run_in_transaction(self, statements: list[Statement], name: str, *, applied: bool) -> None:
        # In SQLite and PostgreSQL, a schema statement is part of the
        # transaction, so a failed migration changes nothing. MySQL commits
        # each schema statement immediately, so there the rollback cannot undo them.
        self.database.begin()
        try:
            for sql, params in statements:
                self.database.execute(sql, params)
            check = self.editor.check_sql()
            if check is not None:
                problems = self.database.execute(check).fetchall()
                if problems:
                    raise MigrationError(f"The migration {name} breaks foreign keys: {problems}")
            if applied:
                self.recorder.record_applied(name)
            else:
                self.recorder.record_unapplied(name)
        except BaseException:
            self.database.rollback()
            raise
        self.database.commit()


def forwards_statements(migration: Migration, state: SchemaState, editor: SchemaEditor) -> list[Statement]:
    """Return the SQL that applies the migration. Change ``state`` to the state after it."""
    statements: list[Statement] = []
    for operation in migration.operations:
        statements += operation.forwards(state, editor)
        operation.apply_to_state(state)
    return statements


def backwards_statements(
    migrations: Sequence[Migration], index: int, editor: SchemaEditor
) -> list[Statement]:
    """Return the SQL that unapplies the migration at the index.

    Each operation needs the state before it, so the function replays the
    migrations first. Then it undoes the operations in the reverse order.
    """
    migration = migrations[index]
    state = state_after(migrations[:index])
    states_before = []
    for operation in migration.operations:
        states_before.append(state.copy())
        operation.apply_to_state(state)
    statements: list[Statement] = []
    for operation, state_before in zip(reversed(migration.operations), reversed(states_before), strict=True):
        statements += operation.backwards(state_before, editor)
    return statements


def find_migration(migrations: Sequence[Migration], target: str) -> int:
    """Return the index of the migration with the name, or with a unique name prefix."""
    for index, migration in enumerate(migrations):
        if migration.name == target:
            return index
    matches = [index for index, migration in enumerate(migrations) if migration.name.startswith(target)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise MigrationError(f"No migration has the name {target!r}.")
    raise MigrationError(f"More than one migration starts with {target!r}.")
