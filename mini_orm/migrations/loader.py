"""The migration loader: it reads the migration files of a directory.

A migration file has a name like "0002_add_books_isbn.py". It defines a
class named Migration, a subclass of mini_orm.migrations.Migration.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from ..exceptions import MigrationError
from .migration import Migration

FILE_NAME = re.compile(r"^\d{4}_\w+\.py$")


def load_migrations(directory: str | Path) -> list[Migration]:
    """Load the migration files of the directory. Return the migrations in order.

    Python runs each migration file to read it, so load only files that you trust.
    A directory that does not exist has no migrations.
    """
    directory = Path(directory)
    migrations: dict[str, Migration] = {}
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if FILE_NAME.match(path.name):
                migration = _load_file(path)
                migrations[migration.name] = migration
    return _in_chain_order(migrations)


def _load_file(path: Path) -> Migration:
    """Run a migration file as a module and return an instance of its Migration class."""
    spec = importlib.util.spec_from_file_location(f"_mini_orm_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Python cannot load {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration_class = getattr(module, "Migration", None)
    if not (isinstance(migration_class, type) and issubclass(migration_class, Migration)):
        raise MigrationError(f"{path} has no class Migration that is a subclass of migrations.Migration.")
    return migration_class(path.stem)


def _in_chain_order(migrations: dict[str, Migration]) -> list[Migration]:
    """Check that the migrations make one chain, and return them in chain order.

    The history of mini_orm is a straight line. Django also permits branches
    and merge migrations, but mini_orm does not. So the rules are simple:

    * exactly one migration has no dependency (the first one)
    * each other migration depends on exactly one migration that exists
    * no two migrations depend on the same migration
    """
    if not migrations:
        return []
    first = [migration for migration in migrations.values() if not migration.dependencies]
    if len(first) != 1:
        names = ", ".join(migration.name for migration in first) or "none"
        raise MigrationError(f"Exactly one migration must have no dependency. These have none: {names}.")

    next_migration: dict[str, Migration] = {}
    for migration in migrations.values():
        if len(migration.dependencies) > 1:
            raise MigrationError(f"{migration.name} has more than one dependency.")
        for dependency in migration.dependencies:
            if dependency not in migrations:
                raise MigrationError(f"{migration.name} depends on {dependency}, which does not exist.")
            if dependency in next_migration:
                other = next_migration[dependency].name
                raise MigrationError(
                    f"{other} and {migration.name} both depend on {dependency}. "
                    "Change one of them to depend on the other."
                )
            next_migration[dependency] = migration

    ordered = [first[0]]
    while ordered[-1].name in next_migration:
        ordered.append(next_migration[ordered[-1].name])
    if len(ordered) != len(migrations):
        raise MigrationError("The dependencies of some migrations make a circle.")
    return ordered
