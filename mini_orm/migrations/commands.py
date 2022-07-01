"""The migration commands. They work like the Django management commands.

* makemigrations: compare the models with the migration files and write a new file
* migrate: apply or unapply migrations
* showmigrations: list the migrations and show which ones are applied
* sqlmigrate: show the SQL of a migration, without running it
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NamedTuple

from ..dialects import SQLITE
from ..models import Model
from .autodetector import detect_changes
from .executor import MigrationExecutor, backwards_statements, find_migration, forwards_statements
from .loader import load_migrations
from .operations import Operation
from .schema_editor import schema_editor_for
from .state import SchemaState, state_after
from .writer import write_migration

if TYPE_CHECKING:
    from ..database import Database
    from ..dialects import Dialect


class NewMigration(NamedTuple):
    path: Path
    operations: list[Operation]
    notes: list[str]


def makemigrations(
    models: Iterable[type[Model]],
    directory: str | Path,
    name: str | None = None,
) -> NewMigration | None:
    """Write a migration file for the changes of the models. Return None if nothing changed.

    The function compares the state of the migration files with the models.
    It does not use a database.
    """
    models = list(models)
    migrations = load_migrations(directory)
    changes = detect_changes(state_after(migrations), SchemaState.from_models(models), models)
    if not changes.operations:
        return None
    number = len(migrations) + 1
    suffix = name or _suggest_name(changes.operations, number)
    dependencies = [migrations[-1].name] if migrations else []
    path = write_migration(
        directory, f"{number:04d}_{suffix}", dependencies, changes.operations, changes.notes
    )
    return NewMigration(path, changes.operations, changes.notes)


def migrate(database: Database, directory: str | Path, target: str | None = None) -> list[str]:
    """Apply or unapply migrations. Return one message for each migration."""
    return MigrationExecutor(database, load_migrations(directory)).migrate(target)


def showmigrations(database: Database, directory: str | Path) -> list[tuple[str, bool]]:
    """Return (name, applied) for each migration, in order."""
    executor = MigrationExecutor(database, load_migrations(directory))
    applied = {migration.name for migration in executor.applied_migrations()}
    return [(migration.name, migration.name in applied) for migration in executor.migrations]


def sqlmigrate(
    directory: str | Path,
    target: str,
    *,
    backwards: bool = False,
    dialect: Dialect = SQLITE,
) -> str:
    """Return the SQL that migrate runs for one migration on a database. It does not use a database."""
    migrations = load_migrations(directory)
    index = find_migration(migrations, target)
    editor = schema_editor_for(dialect)
    if backwards:
        statements = backwards_statements(migrations, index, editor)
    else:
        statements = forwards_statements(migrations[index], state_after(migrations[:index]), editor)
    lines = [f"{sql};" for sql in editor.before_migration()]
    lines.append("BEGIN;")
    for sql, params in statements:
        lines.append(f"{sql};" + (f"  -- params: {params!r}" if params else ""))
    lines.append("COMMIT;")
    lines += [f"{sql};" for sql in editor.after_migration()]
    return "\n".join(lines)


def models_in_module(module: ModuleType) -> list[type[Model]]:
    """Return the model classes with a table that a module defines or imports."""
    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Model)
        and value is not Model
        and not value._meta.abstract
    ]


def _suggest_name(operations: list[Operation], number: int) -> str:
    if number == 1:
        return "initial"
    name = operations[0].name_fragment()
    if len(operations) > 1:
        name += "_and_more"
    return name
