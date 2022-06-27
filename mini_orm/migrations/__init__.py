"""Schema migrations for mini_orm, in the style of Django.

A migration is a Python file with a list of operations that change the
schema. makemigrations writes the file. migrate runs it.

    from mini_orm import migrations

    migrations.makemigrations([Author, Book], "blog/migrations")
    migrations.migrate(db, "blog/migrations")

The command line interface does the same:

    python -m mini_orm.migrations makemigrations --models blog.models --dir blog/migrations
    python -m mini_orm.migrations migrate --dir blog/migrations --db blog.db
    python -m mini_orm.migrations migrate --dir blog/migrations --db postgresql://user:password@localhost/blog

The parts, in the order that a reader can follow:

* state.py: the schema state, made from the migration files or the models
* operations.py: the operations, for example AddColumn: what changes
* schema_editor.py: the SQL of each change for each database: how it changes
* autodetector.py: the operations between two states (makemigrations)
* writer.py: the Python source of a migration file
* loader.py: the migration files, in chain order
* recorder.py: the table of the applied migrations
* executor.py: apply and unapply migrations (migrate)
* commands.py and cli.py: the commands
"""

from ..schema import Column
from .commands import NewMigration, makemigrations, migrate, models_in_module, showmigrations, sqlmigrate
from .migration import Migration
from .operations import (
    AddColumn,
    AlterColumn,
    CreateTable,
    DropColumn,
    DropTable,
    Operation,
    RenameColumn,
    RenameTable,
    RunSQL,
)
from .state import SchemaState

__all__ = [
    "AddColumn",
    "AlterColumn",
    "Column",
    "CreateTable",
    "DropColumn",
    "DropTable",
    "Migration",
    "NewMigration",
    "Operation",
    "RenameColumn",
    "RenameTable",
    "RunSQL",
    "SchemaState",
    "makemigrations",
    "migrate",
    "models_in_module",
    "showmigrations",
    "sqlmigrate",
]
