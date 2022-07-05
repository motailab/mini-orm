"""The command line interface of the migrations.

Usage:

    python -m mini_orm.migrations makemigrations --models blog.models --dir blog/migrations
    python -m mini_orm.migrations migrate --dir blog/migrations --db blog.db [TARGET]
    python -m mini_orm.migrations showmigrations --dir blog/migrations --db blog.db
    python -m mini_orm.migrations sqlmigrate --dir blog/migrations NAME [--backwards] [--dialect mysql]

The --db option takes an SQLite file name or a database URL, for example
"postgresql://user:password@localhost:5432/blog".
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

from ..database import Database
from ..dialects import MySQLDialect, PostgreSQLDialect, SQLiteDialect
from ..exceptions import ORMError
from .commands import makemigrations, migrate, models_in_module, showmigrations, sqlmigrate

DIALECTS = {"sqlite": SQLiteDialect, "postgresql": PostgreSQLDialect, "mysql": MySQLDialect}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Return the exit status: 0 for success, 1 for an error."""
    parser = _make_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except ORMError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mini_orm.migrations", description="Schema migrations.")
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    make = commands.add_parser("makemigrations", help="write a migration file for the model changes")
    make.add_argument("--models", required=True, help="the module with the models, for example blog.models")
    make.add_argument("--dir", required=True, help="the directory of the migration files")
    make.add_argument("--name", help="the name of the migration, without the number")
    make.set_defaults(handler=_makemigrations)

    run = commands.add_parser("migrate", help="apply or unapply migrations")
    run.add_argument("--dir", required=True, help="the directory of the migration files")
    run.add_argument("--db", required=True, help="an SQLite file name or a database URL")
    run.add_argument("--echo", action="store_true", help="print each SQL statement")
    run.add_argument("target", nargs="?", help='the last migration to keep applied, or "zero"')
    run.set_defaults(handler=_migrate)

    show = commands.add_parser("showmigrations", help="list the migrations")
    show.add_argument("--dir", required=True, help="the directory of the migration files")
    show.add_argument("--db", required=True, help="an SQLite file name or a database URL")
    show.set_defaults(handler=_showmigrations)

    sql = commands.add_parser("sqlmigrate", help="show the SQL of a migration")
    sql.add_argument("--dir", required=True, help="the directory of the migration files")
    sql.add_argument("name", help="the migration name or a unique prefix, for example 0002")
    sql.add_argument("--backwards", action="store_true", help="show the SQL that unapplies the migration")
    sql.add_argument("--dialect", choices=sorted(DIALECTS), default="sqlite", help="the database of the SQL")
    sql.set_defaults(handler=_sqlmigrate)
    return parser


def _makemigrations(args: argparse.Namespace) -> None:
    models = models_in_module(importlib.import_module(args.models))
    result = makemigrations(models, args.dir, args.name)
    if result is None:
        print("No changes.")
        return
    print(f"Wrote {result.path}")
    for operation in result.operations:
        print(f"  - {operation.describe()}")
    for note in result.notes:
        print(f"NOTE: {note}")


def _migrate(args: argparse.Namespace) -> None:
    with Database(args.db, echo=args.echo) as database:
        messages = migrate(database, args.dir, args.target)
    print("\n".join(messages) if messages else "No migrations to apply or unapply.")


def _showmigrations(args: argparse.Namespace) -> None:
    with Database(args.db) as database:
        for name, applied in showmigrations(database, args.dir):
            print(f"[{'X' if applied else ' '}] {name}")


def _sqlmigrate(args: argparse.Namespace) -> None:
    print(sqlmigrate(args.dir, args.name, backwards=args.backwards, dialect=DIALECTS[args.dialect]()))
