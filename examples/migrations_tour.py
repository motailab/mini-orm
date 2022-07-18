"""A tour of the migrations. Run it and read the migration files and the SQL.

    python examples/migrations_tour.py                     # SQLite
    python examples/migrations_tour.py "postgresql://..."  # an empty PostgreSQL database
    python examples/migrations_tour.py "mysql://..."       # an empty MySQL database

The tour uses a temporary directory for the migration files. At the end,
it unapplies all migrations and drops the table of the applied migrations,
so the database is as it was before the tour.
"""

import sys
import tempfile
from pathlib import Path

from mini_orm import Database, ForeignKey, IntegerField, Model, Session, StringField, migrations

# ---------------------------------------------------------------------------
# Version 1 of the models.
# ---------------------------------------------------------------------------


class Author(Model):
    __tablename__ = "authors"
    name = StringField(null=False)


class Book(Model):
    __tablename__ = "books"
    title = StringField(null=False)
    author = ForeignKey(Author, on_delete="CASCADE")


# ---------------------------------------------------------------------------
# Version 2: the developer changed the models. Same tables, new class names.
# ---------------------------------------------------------------------------


class AuthorV2(Model):
    __tablename__ = "authors"
    name = StringField(null=False)
    email = StringField()  # New. It can be NULL.


class BookV2(Model):
    __tablename__ = "books"
    title = StringField(max_length=200, null=False)  # Changed: TEXT -> VARCHAR(200)
    stock = IntegerField(default=0, null=False)  # New. NOT NULL, so the old rows get the default.
    author = ForeignKey(AuthorV2, on_delete="CASCADE")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def rows(db: Database, table: str) -> list[tuple]:
    """Return all rows of a table. Some drivers give a tuple of rows, so make a list."""
    return [tuple(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]


def main(url: str | None = None) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "migrations"
        db = Database(url or str(Path(temporary) / "shop.db"))
        existing = {"authors", "books", "mini_orm_migrations"} & set(db.table_names())
        if existing:
            print(f"The database has tables of the tour already: {sorted(existing)}. Use an empty database.")
            return

        section("1. makemigrations: the first migration file")
        result = migrations.makemigrations([Author, Book], directory)
        print(result.path.read_text())

        section("2. migrate: apply the migration (echo shows the SQL)")
        db.echo = True
        print(migrations.migrate(db, directory))
        db.echo = False
        with Session(db) as session:
            abdul = Author(name="Abdul")
            session.add_all([abdul, Book(title="SQL Basics", author=abdul)])
            session.commit()
        print(f"-- rows of books: {rows(db, 'books')}")

        section("3. The models changed: makemigrations finds the changes")
        result = migrations.makemigrations([AuthorV2, BookV2], directory)
        print(result.path.read_text())

        section(f"4. sqlmigrate: the SQL of the new migration for {db.dialect.name}, without running it")
        print(migrations.sqlmigrate(directory, "0002", dialect=db.dialect))

        section("5. migrate: apply the new migration. The rows stay.")
        print(migrations.migrate(db, directory))
        print(f"-- rows of books: {rows(db, 'books')}")
        print(f"-- rows of authors: {rows(db, 'authors')}")

        section("6. showmigrations: [X] means applied")
        for name, applied in migrations.showmigrations(db, directory):
            print(f"[{'X' if applied else ' '}] {name}")

        section("7. migrate backwards to 0001: the new columns go away, the rows stay")
        print(migrations.migrate(db, directory, "0001"))
        print(f"-- rows of books: {rows(db, 'books')}")

        # Leave the database as it was before the tour.
        migrations.migrate(db, directory, "zero")
        db.execute(db.dialect.drop_table_sql("mini_orm_migrations"))
        db.close()


if __name__ == "__main__":
    main(*sys.argv[1:2])
