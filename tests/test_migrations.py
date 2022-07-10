"""Tests for the migration commands, with migration files and a database.

The tests that use the ``db`` fixture run once for each available database.
"""

import textwrap

import pytest

from mini_orm import (
    DatabaseError,
    FloatField,
    ForeignKey,
    IntegrityError,
    IrreversibleError,
    MigrationError,
    Model,
    MySQLDialect,
    PostgreSQLDialect,
    Session,
    StringField,
)
from mini_orm.migrations import (
    AddColumn,
    AlterColumn,
    Column,
    RenameColumn,
    RenameTable,
    RunSQL,
    makemigrations,
    migrate,
    showmigrations,
    sqlmigrate,
)
from mini_orm.migrations.cli import main
from mini_orm.migrations.writer import write_migration

from .conftest import dialect_sql

# Version 1 and version 2 of the same two tables.


class AuthorV1(Model):
    __tablename__ = "authors"
    name = StringField(null=False)


class BookV1(Model):
    __tablename__ = "books"
    title = StringField()
    author = ForeignKey(AuthorV1, on_delete="CASCADE")


class AuthorV2(Model):
    __tablename__ = "authors"
    name = StringField(max_length=100, null=False)  # TEXT -> VARCHAR(100)
    email = StringField()  # A new column that can be NULL


class BookV2(Model):
    __tablename__ = "books"
    title = StringField()
    price = FloatField(default=9.5, null=False)  # A new NOT NULL column with a default
    author = ForeignKey(AuthorV2, on_delete="CASCADE")


V1 = [AuthorV1, BookV1]
V2 = [AuthorV2, BookV2]


@pytest.fixture
def directory(tmp_path):
    return tmp_path / "migrations"


@pytest.fixture
def db(empty_db):
    return empty_db


def run(db, sql, params=()):
    """Run a statement of the SQLite style on the database. Return its rows as a list.

    cursor.description is None for a statement that returns no rows, for
    example DELETE. Then psycopg refuses fetchall(), so the helper does not call it.
    """
    cursor = db.execute(dialect_sql(db, sql), params)
    return list(cursor.fetchall()) if cursor.description is not None else []


def add_alice_and_her_book(db, author_model, book_model):
    with Session(db) as session:
        alice = author_model(name="Alice")
        session.add_all([alice, book_model(title="SQL Basics", author=alice)])
        session.commit()


# -- makemigrations ------------------------------------------------------------------


def test_makemigrations_writes_the_initial_migration(directory):
    result = makemigrations(V1, directory)
    assert result.path.name == "0001_initial.py"
    assert [operation.describe() for operation in result.operations] == [
        "Create table authors",
        "Create table books",
    ]
    assert makemigrations(V1, directory) is None  # No changes the second time.


def test_makemigrations_names_the_next_migration(directory):
    makemigrations(V1, directory)
    result = makemigrations(V2, directory)
    assert result.path.name == "0002_alter_authors_name_and_more.py"
    assert "dependencies = [\n        '0001_initial',\n    ]" in result.path.read_text()
    assert makemigrations(V2, directory, name="custom") is None
    makemigrations(V1, directory, name="back_to_v1")
    assert (directory / "0003_back_to_v1.py").exists()


# -- migrate ---------------------------------------------------------------------------


def test_migrate_applies_the_migrations_and_records_them(db, directory):
    makemigrations(V1, directory)
    assert migrate(db, directory) == ["Applied 0001_initial"]
    assert db.table_names() == ["authors", "books", "mini_orm_migrations"]
    assert showmigrations(db, directory) == [("0001_initial", True)]
    assert migrate(db, directory) == []  # Nothing more to apply.


def test_migrate_keeps_the_rows_when_the_columns_change(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    add_alice_and_her_book(db, AuthorV1, BookV1)

    makemigrations(V2, directory)
    assert migrate(db, directory) == ["Applied 0002_alter_authors_name_and_more"]
    assert db.column_names("authors") == ["id", "name", "email"]
    assert run(db, 'SELECT * FROM "authors"') == [(1, "Alice", None)]
    assert run(db, 'SELECT * FROM "books"') == [(1, "SQL Basics", 1, 9.5)]

    # The ORM works with the new models.
    with Session(db) as session:
        book = session.query(BookV2).one()
        assert (book.price, book.author.name) == (9.5, "Alice")


def test_migrate_backwards_to_a_target_and_to_zero(db, directory):
    makemigrations(V1, directory)
    makemigrations(V2, directory)
    migrate(db, directory)
    add_alice_and_her_book(db, AuthorV2, BookV2)

    assert migrate(db, directory, "0001") == ["Unapplied 0002_alter_authors_name_and_more"]
    assert db.column_names("books") == ["id", "title", "author_id"]
    assert run(db, 'SELECT * FROM "books"') == [(1, "SQL Basics", 1)]
    assert showmigrations(db, directory) == [
        ("0001_initial", True),
        ("0002_alter_authors_name_and_more", False),
    ]

    assert migrate(db, directory, "zero") == ["Unapplied 0001_initial"]
    assert db.table_names() == ["mini_orm_migrations"]


def test_a_column_change_keeps_the_child_rows_and_the_foreign_key(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    add_alice_and_her_book(db, AuthorV1, BookV1)
    makemigrations(V2, directory)
    migrate(db, directory)  # On SQLite, the table remake drops and makes "authors" again.

    # Without PRAGMA foreign_keys = OFF, the DROP TABLE of SQLite deletes the book (CASCADE).
    assert run(db, 'SELECT COUNT(*) FROM "books"') == [(1,)]
    # The foreign key still works on the changed table.
    run(db, 'DELETE FROM "authors"')
    assert run(db, 'SELECT COUNT(*) FROM "books"') == [(0,)]
    if db.dialect.name == "sqlite":
        assert run(db, "PRAGMA foreign_keys") == [(1,)]


def test_a_failed_migration_is_atomic_except_on_mysql(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    run(db, 'INSERT INTO "books" ("title") VALUES (NULL)')
    # A hand-written migration with two operations. The second one fails,
    # because "title" becomes NOT NULL and a row has NULL.
    operations = [AddColumn("books", Column("isbn", "string", max_length=13)), _title_not_null()]
    write_migration(directory, "0002_isbn_and_title", ["0001_initial"], operations)

    with pytest.raises(DatabaseError):
        migrate(db, directory)
    assert showmigrations(db, directory)[1] == ("0002_isbn_and_title", False)
    assert not db.in_transaction
    run(db, 'INSERT INTO "books" ("title") VALUES (NULL)')  # "title" still accepts NULL.
    if db.dialect.name == "mysql":
        # MySQL commits each schema statement, so the first change stays.
        assert "isbn" in db.column_names("books")
    else:
        # SQLite and PostgreSQL roll back the schema statements too.
        assert "isbn" not in db.column_names("books")


def test_alter_column_default_fills_the_null_values(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    run(db, 'INSERT INTO "books" ("title") VALUES (NULL)')
    write_migration(directory, "0002_title_not_null", ["0001_initial"], [_title_not_null(default="Untitled")])
    migrate(db, directory)
    assert run(db, 'SELECT "title" FROM "books"') == [("Untitled",)]


def test_a_row_that_breaks_a_foreign_key_rolls_back_the_migration(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    orphan = RunSQL(dialect_sql(db, """INSERT INTO "books" ("title", "author_id") VALUES ('Orphan', 999)"""))
    write_migration(directory, "0002_orphan", ["0001_initial"], [orphan])
    # SQLite: the checks are off, so PRAGMA foreign_key_check finds the row
    # before the COMMIT. PostgreSQL and MySQL refuse the INSERT at once.
    with pytest.raises((MigrationError, IntegrityError)):
        migrate(db, directory)
    assert run(db, 'SELECT COUNT(*) FROM "books"') == [(0,)]


def test_run_sql_and_irreversible_migrations(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    add_alice_and_her_book(db, AuthorV1, BookV1)
    upper = RunSQL(
        dialect_sql(db, """UPDATE "authors" SET "name" = upper("name")"""),
        reverse_sql=dialect_sql(db, """UPDATE "authors" SET "name" = 'Alice'"""),
    )
    write_migration(directory, "0002_upper", ["0001_initial"], [upper])
    write_migration(directory, "0003_no_reverse", ["0002_upper"], [RunSQL("SELECT 1")])
    migrate(db, directory)
    assert run(db, 'SELECT "name" FROM "authors"') == [("ALICE",)]

    with pytest.raises(IrreversibleError):
        migrate(db, directory, "0001")
    assert showmigrations(db, directory)[2] == ("0003_no_reverse", True)


def test_renames_keep_the_data(db, directory):
    makemigrations(V1, directory)
    migrate(db, directory)
    add_alice_and_her_book(db, AuthorV1, BookV1)
    renames = [RenameColumn("books", "title", "name"), RenameTable("authors", "writers")]
    write_migration(directory, "0002_renames", ["0001_initial"], renames)
    migrate(db, directory)
    assert run(db, 'SELECT "name" FROM "books"') == [("SQL Basics",)]
    assert run(db, 'SELECT "name" FROM "writers"') == [("Alice",)]
    # The database changed the foreign key of "books" to refer to "writers".
    run(db, 'DELETE FROM "writers"')
    assert run(db, 'SELECT COUNT(*) FROM "books"') == [(0,)]

    migrate(db, directory, "0001")
    assert db.column_names("books") == ["id", "title", "author_id"]
    assert "authors" in db.table_names()


class LabelV1(Model):
    __tablename__ = "labels"
    title = StringField(null=False)


class LabelV2(Model):
    __tablename__ = "labels"
    name = StringField(null=False)  # The developer renamed "title".


def test_rename_procedure_of_the_guide(db, directory):
    makemigrations([LabelV1], directory)
    migrate(db, directory)
    run(db, """INSERT INTO "labels" ("title") VALUES ('Fragile')""")

    # Step 2: makemigrations. A rename of a NOT NULL column gives a note, not an error.
    result = makemigrations([LabelV2], directory)
    assert 'RenameColumn("labels", "title", "name")' in result.notes[0]
    # Steps 4 to 6: replace AddColumn and DropColumn with RenameColumn.
    lines = result.path.read_text().splitlines()
    rename = "        migrations.RenameColumn('labels', 'title', 'name'),"
    lines = [line for line in lines if "migrations.DropColumn(" not in line]
    lines = [rename if "migrations.AddColumn(" in line else line for line in lines]
    result.path.write_text("\n".join(lines) + "\n")
    # Steps 7 and 8: sqlmigrate, then migrate.
    assert 'RENAME COLUMN "title" TO "name"' in sqlmigrate(directory, "0002")
    migrate(db, directory)
    assert run(db, 'SELECT "name" FROM "labels"') == [("Fragile",)]


# -- sqlmigrate ------------------------------------------------------------------------


def test_sqlmigrate_shows_the_sql_without_a_database(directory):
    makemigrations(V1, directory)
    makemigrations(V2, directory)
    forwards = sqlmigrate(directory, "0002")
    assert forwards.startswith('PRAGMA foreign_keys = OFF;\nBEGIN;\nCREATE TABLE "new__authors"')
    assert "-- params: [9.5]" in forwards
    assert forwards.endswith("COMMIT;\nPRAGMA foreign_keys = ON;")
    backwards = sqlmigrate(directory, "0002", backwards=True)
    assert 'INSERT INTO "new__books" ("id", "title", "author_id")' in backwards
    with pytest.raises(MigrationError, match="No migration"):
        sqlmigrate(directory, "0009")
    with pytest.raises(MigrationError, match="More than one"):
        sqlmigrate(directory, "000")


def test_sqlmigrate_for_postgresql_and_mysql(directory):
    makemigrations(V1, directory)
    makemigrations(V2, directory)
    postgresql = sqlmigrate(directory, "0002", dialect=PostgreSQLDialect())
    assert postgresql.startswith("BEGIN;\n")  # No PRAGMA: PostgreSQL changes columns in place.
    assert 'ALTER TABLE "books" ADD COLUMN "price" DOUBLE PRECISION;' in postgresql
    assert 'UPDATE "books" SET "price" = %s;  -- params: [9.5]' in postgresql
    mysql = sqlmigrate(directory, "0002", dialect=MySQLDialect())
    assert "ALTER TABLE `books` MODIFY COLUMN `price` DOUBLE NOT NULL;" in mysql


# -- loader and history ------------------------------------------------------------------


def test_loader_errors(tmp_path):
    write_migration(tmp_path / "a", "0001_initial", [], [])
    write_migration(tmp_path / "a", "0002_x", ["0001_initial"], [])
    write_migration(tmp_path / "a", "0003_y", ["0001_initial"], [])
    with pytest.raises(MigrationError, match="0002_x and 0003_y both depend on 0001_initial"):
        makemigrations(V1, tmp_path / "a")

    write_migration(tmp_path / "b", "0001_initial", [], [])
    write_migration(tmp_path / "b", "0002_x", ["0009_missing"], [])
    with pytest.raises(MigrationError, match="depends on 0009_missing, which does not exist"):
        makemigrations(V1, tmp_path / "b")

    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "0001_initial.py").write_text("x = 1\n")
    with pytest.raises(MigrationError, match="has no class Migration"):
        makemigrations(V1, tmp_path / "c")


def test_history_must_match_the_files(db, directory, tmp_path):
    makemigrations(V1, directory)
    migrate(db, directory)
    with pytest.raises(MigrationError, match="not consistent"):
        migrate(db, tmp_path / "empty")


def test_migrate_refuses_an_open_session_transaction(db, directory):
    makemigrations(V1, directory)
    with Session(db) as session:
        session.execute("SELECT 1")  # The session opens a transaction.
        with pytest.raises(MigrationError, match="open transaction"):
            migrate(db, directory)


# -- Command line ------------------------------------------------------------------------


def test_command_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "cli_models.py").write_text(
        textwrap.dedent(
            """
            from mini_orm import Model, StringField

            class Note(Model):
                __tablename__ = "notes"
                text = StringField()
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    folder = str(tmp_path / "migrations")
    database = str(tmp_path / "notes.db")

    assert main(["makemigrations", "--models", "cli_models", "--dir", folder]) == 0
    assert "Create table notes" in capsys.readouterr().out
    assert main(["migrate", "--dir", folder, "--db", database]) == 0
    assert capsys.readouterr().out == "Applied 0001_initial\n"
    assert main(["showmigrations", "--dir", folder, "--db", database]) == 0
    assert capsys.readouterr().out == "[X] 0001_initial\n"
    assert main(["sqlmigrate", "--dir", folder, "0001"]) == 0
    assert 'CREATE TABLE "notes"' in capsys.readouterr().out
    assert main(["migrate", "--dir", folder, "--db", database, "0009"]) == 1
    assert "No migration" in capsys.readouterr().err


def _title_not_null(default=None):
    return AlterColumn("books", Column("title", "string", null=False), default=default)
