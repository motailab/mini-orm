"""The base class of a migration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .operations import Operation


class Migration:
    """The base class of the class ``Migration`` in each migration file.

    A migration file looks like this:

        from mini_orm import migrations


        class Migration(migrations.Migration):
            dependencies = ["0001_initial"]
            operations = [
                migrations.AddColumn("books", migrations.Column("isbn", "TEXT")),
            ]

    The loader makes an instance and gives it the name of the file.
    """

    dependencies: Sequence[str] = ()
    operations: Sequence[Operation] = ()

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<Migration {self.name}>"
