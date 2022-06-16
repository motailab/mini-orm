"""Schema descriptions: the Column class and the CREATE TABLE of a model.

A Column describes one column without a database: "integer", "string",
"boolean" and so on. The dialect gives the SQL text for each database.
create_table_sql() makes the columns from the fields of a model. The
migrations keep columns in their migration files, so one migration file
works with SQLite, PostgreSQL and MySQL.

Example: for this model

    class Book(Model):
        __tablename__ = "books"
        title = StringField(max_length=200, null=False)
        author = ForeignKey(Author, on_delete="CASCADE")

create_table_sql(Book) gives, for SQLite:

    CREATE TABLE IF NOT EXISTS "books" (
        "id" INTEGER PRIMARY KEY NOT NULL,
        "title" VARCHAR(200) NOT NULL,
        "author_id" INTEGER,
        FOREIGN KEY ("author_id") REFERENCES "authors" ("id") ON DELETE CASCADE
    )
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .dialects import SQLITE
from .graph import sort_by_dependencies
from .relations import ForeignKey

if TYPE_CHECKING:
    from .dialects import Dialect
    from .fields import Field
    from .models import Model

# The column types. Each dialect has an SQL type for each of them.
COLUMN_TYPES = ("integer", "float", "string", "boolean", "datetime", "date")


@dataclass(frozen=True)
class Column:
    """The description of one column. It does not depend on the database.

    The class is a frozen dataclass, so two columns with the same values are
    equal. The migrations compare columns to find the changes of a table.
    """

    name: str
    type: str  # One of COLUMN_TYPES.
    max_length: int | None = None  # For "string": VARCHAR(max_length). Without it: TEXT.
    primary_key: bool = False
    null: bool = True
    unique: bool = False
    # For a foreign key: the (table, column) that the column refers to.
    references: tuple[str, str] | None = None
    on_delete: str | None = None

    @classmethod
    def from_field(cls, field: Field) -> Column:
        """Make the column description of a field."""
        references = None
        on_delete = None
        if isinstance(field, ForeignKey):
            target = field.target_model._meta
            references = (target.table_name, target.primary_key.column)
            on_delete = field.on_delete
        return cls(
            name=field.column,
            type=field.column_type,
            max_length=field.max_length,
            primary_key=field.primary_key,
            # A primary key is always NOT NULL in the table. In Python, it can
            # be None until the database gives it a value.
            null=field.null and not field.primary_key,
            unique=field.unique and not field.primary_key,
            references=references,
            on_delete=on_delete,
        )


def table_columns(model: type[Model]) -> list[Column]:
    """Return the column descriptions of a model, in field order."""
    return [Column.from_field(field) for field in model._meta.fields.values()]


def create_table_sql(model: type[Model], dialect: Dialect = SQLITE) -> str:
    """Make the CREATE TABLE statement of a model for a database."""
    return dialect.create_table_sql(model._meta.table_name, table_columns(model))


def drop_table_sql(model: type[Model], dialect: Dialect = SQLITE) -> str:
    """Make the DROP TABLE statement of a model for a database."""
    return dialect.drop_table_sql(model._meta.table_name)


def sort_models(models: Iterable[type[Model]]) -> list[type[Model]]:
    """Sort models so that a model comes after the models that it refers to.

    Create the tables in this order. Drop the tables in the reverse order.
    """

    def referenced_models(model: type[Model]) -> list[type[Model]]:
        return [foreign_key.target_model for foreign_key in model._meta.foreign_keys]

    return sort_by_dependencies(models, referenced_models)
