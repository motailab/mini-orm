"""Tests for the parts of the migrations that need no database: state, operations,
autodetector and writer."""

import datetime

import pytest

from mini_orm import (
    BooleanField,
    DateField,
    IrreversibleError,
    MigrationError,
    Model,
    MySQLDialect,
    PostgreSQLDialect,
    SQLiteDialect,
    StringField,
)
from mini_orm.migrations import (
    AddColumn,
    AlterColumn,
    Column,
    CreateTable,
    DropColumn,
    DropTable,
    RenameColumn,
    RenameTable,
    RunSQL,
    SchemaState,
)
from mini_orm.migrations.autodetector import detect_changes
from mini_orm.migrations.loader import load_migrations
from mini_orm.migrations.schema_editor import schema_editor_for
from mini_orm.migrations.writer import migration_source, write_migration

from .example_models import Author, Book, Country

SQLITE = schema_editor_for(SQLiteDialect())
POSTGRESQL = schema_editor_for(PostgreSQLDialect())
MYSQL = schema_editor_for(MySQLDialect())

ID = Column("id", "integer", primary_key=True, null=False)
NAME = Column("name", "string", null=False)
AUTHOR_ID = Column("author_id", "integer", references=("authors", "id"), on_delete="CASCADE")


def shop_state() -> SchemaState:
    return SchemaState(
        {
            "authors": [ID, NAME],
            "books": [ID, Column("title", "string"), AUTHOR_ID],
        }
    )


# -- Column and SchemaState ---------------------------------------------------


def test_column_from_field():
    assert Column.from_field(Book.id) == Column("id", "integer", primary_key=True, null=False)
    assert Column.from_field(Book.title) == Column("title", "string", null=False)
    assert Column.from_field(Author.email) == Column("email", "string", max_length=100, unique=True)
    assert Column.from_field(Book.author) == AUTHOR_ID
    assert Column.from_field(Country.code) == Column(
        "code", "string", max_length=2, primary_key=True, null=False
    )


def test_state_from_models():
    state = SchemaState.from_models([Author, Book])
    assert list(state.tables) == ["authors", "books"]
    assert [column.name for column in state.columns("books")] == [
        "id",
        "title",
        "price",
        "published",
        "released_on",
        "author_id",
    ]


def test_state_copy_is_independent():
    state = shop_state()
    copy = state.copy()
    copy.columns("authors").append(Column("email", "string"))
    assert len(state.columns("authors")) == 2


def test_state_errors():
    state = shop_state()
    with pytest.raises(MigrationError, match="no table 'reviews'"):
        state.columns("reviews")
    with pytest.raises(MigrationError, match="no column authors.email"):
        state.column("authors", "email")


# -- Operations -----------------------------------------------------------------


def test_create_table():
    state = SchemaState()
    operation = CreateTable("authors", [ID, NAME])
    assert operation.forwards(state, SQLITE) == [
        ('CREATE TABLE "authors" (\n    "id" INTEGER PRIMARY KEY NOT NULL,\n    "name" TEXT NOT NULL\n)', [])
    ]
    assert operation.backwards(state, SQLITE) == [('DROP TABLE "authors"', [])]
    operation.apply_to_state(state)
    assert state.columns("authors") == [ID, NAME]
    with pytest.raises(MigrationError, match="exists already"):
        operation.apply_to_state(state)


def test_drop_table_backwards_makes_the_table_again():
    state = shop_state()
    operation = DropTable("authors")
    assert operation.forwards(state, SQLITE) == [('DROP TABLE "authors"', [])]
    assert operation.backwards(state, SQLITE)[0][0].startswith('CREATE TABLE "authors"')
    operation.apply_to_state(state)
    assert "authors" not in state.tables


def test_add_column_uses_the_table_remake():
    state = shop_state()
    operation = AddColumn("authors", Column("rank", "integer", null=False), default=0)
    statements = operation.forwards(state, SQLITE)
    assert [sql.split(" (")[0].split(" SELECT")[0] for sql, _ in statements] == [
        'CREATE TABLE "new__authors"',
        'INSERT INTO "new__authors"',
        'DROP TABLE "authors"',
        'ALTER TABLE "new__authors" RENAME TO "authors"',
    ]
    assert statements[1] == (
        'INSERT INTO "new__authors" ("id", "name", "rank") SELECT "id", "name", ? FROM "authors"',
        [0],
    )
    operation.apply_to_state(state)
    assert [column.name for column in state.columns("authors")] == ["id", "name", "rank"]


def test_add_column_without_default_copies_null():
    state = shop_state()
    statements = AddColumn("authors", Column("email", "string")).forwards(state, SQLITE)
    assert statements[1] == (
        'INSERT INTO "new__authors" ("id", "name", "email") SELECT "id", "name", NULL FROM "authors"',
        [],
    )


def test_add_column_errors():
    with pytest.raises(MigrationError, match="exists already"):
        AddColumn("authors", NAME).apply_to_state(shop_state())


def test_drop_column():
    state = shop_state()
    operation = DropColumn("books", "title")
    assert operation.forwards(state, SQLITE)[1] == (
        'INSERT INTO "new__books" ("id", "author_id") SELECT "id", "author_id" FROM "books"',
        [],
    )
    backwards_copy = operation.backwards(state, SQLITE)[1]
    assert backwards_copy[0].startswith('INSERT INTO "new__books" ("id", "title", "author_id")')
    operation.apply_to_state(state)
    assert [column.name for column in state.columns("books")] == ["id", "author_id"]
    with pytest.raises(MigrationError, match="no column books.title"):
        operation.forwards(state, SQLITE)


def test_alter_column_fills_the_null_values_with_the_default():
    state = shop_state()
    operation = AlterColumn("books", Column("title", "string", null=False), default="untitled")
    assert operation.forwards(state, SQLITE)[1] == (
        'INSERT INTO "new__books" ("id", "title", "author_id") '
        'SELECT "id", COALESCE("title", ?), "author_id" FROM "books"',
        ["untitled"],
    )
    operation.apply_to_state(state)
    assert state.column("books", "title") == Column("title", "string", null=False)


def test_rename_column_changes_the_references_in_the_state():
    state = shop_state()
    operation = RenameColumn("authors", "id", "author_key")
    assert operation.forwards(state, SQLITE) == [
        ('ALTER TABLE "authors" RENAME COLUMN "id" TO "author_key"', [])
    ]
    assert operation.backwards(state, SQLITE) == [
        ('ALTER TABLE "authors" RENAME COLUMN "author_key" TO "id"', [])
    ]
    operation.apply_to_state(state)
    assert state.column("books", "author_id").references == ("authors", "author_key")


def test_rename_table_changes_the_references_in_the_state():
    state = shop_state()
    operation = RenameTable("authors", "writers")
    assert operation.forwards(state, SQLITE) == [('ALTER TABLE "authors" RENAME TO "writers"', [])]
    operation.apply_to_state(state)
    assert "authors" not in state.tables
    assert state.column("books", "author_id").references == ("writers", "id")


def test_run_sql():
    operation = RunSQL("UPDATE books SET title = 'x'")
    state = shop_state()
    operation.apply_to_state(state)
    assert state.tables == shop_state().tables  # No change of the state.
    assert operation.forwards(state, SQLITE) == [("UPDATE books SET title = 'x'", [])]
    with pytest.raises(IrreversibleError):
        operation.backwards(state, SQLITE)
    assert RunSQL("A", reverse_sql="B").backwards(state, SQLITE) == [("B", [])]


# -- Autodetector -----------------------------------------------------------------


class Publisher(Model):
    __tablename__ = "publishers"
    name = StringField(null=False)
    active = BooleanField(default=True, null=False)


def test_detect_new_tables_in_dependency_order():
    changes = detect_changes(SchemaState(), SchemaState.from_models([Book, Author]), [Book, Author])
    assert [operation.table for operation in changes.operations] == ["authors", "books"]
    assert all(isinstance(operation, CreateTable) for operation in changes.operations)


def test_detect_no_changes():
    state = SchemaState.from_models([Author, Book])
    assert detect_changes(state, state.copy(), [Author, Book]).operations == []


def test_detect_column_changes():
    old = SchemaState({"publishers": [ID, Column("name", "string"), Column("city", "string")]})
    changes = detect_changes(old, SchemaState.from_models([Publisher]), [Publisher])
    assert changes.operations == [
        AlterColumn("publishers", Column("name", "string", null=False)),
        AddColumn("publishers", Column("active", "boolean", null=False), default=True),
        DropColumn("publishers", "city"),
    ]


def test_new_not_null_column_needs_a_constant_default():
    class Box(Model):
        __tablename__ = "boxes"
        label = StringField(null=False)

    old = SchemaState({"boxes": [ID]})
    with pytest.raises(MigrationError, match="NOT NULL and has no constant default"):
        detect_changes(old, SchemaState.from_models([Box]), [Box])


def test_detect_dropped_tables_children_first():
    old = shop_state()
    changes = detect_changes(old, SchemaState(), [])
    assert changes.operations == [DropTable("books"), DropTable("authors")]


def test_possible_renames_give_notes():
    old = SchemaState({"boxes": [ID, Column("label", "string")]})
    new = SchemaState({"boxes": [ID, Column("title", "string")]})
    notes = detect_changes(old, new, []).notes
    assert len(notes) == 1
    assert 'RenameColumn("boxes", "label", "title")' in notes[0]

    notes = detect_changes(SchemaState({"boxes": [ID]}), SchemaState({"crates": [ID]}), []).notes
    assert 'RenameTable("boxes", "crates")' in notes[0]


# -- Writer -----------------------------------------------------------------------


def test_writer_source_is_valid_python_and_loads_again(tmp_path):
    operations = [
        CreateTable("authors", [ID, NAME]),
        AddColumn("books", Column("price", "float", null=False), default=9.5),
        RenameColumn("books", "title", "name"),
        RunSQL("UPDATE books SET price = 1\nWHERE price IS NULL", reverse_sql="SELECT 1"),
    ]
    write_migration(tmp_path, "0001_initial", [], operations, notes=["A note."])
    [migration] = load_migrations(tmp_path)
    assert migration.name == "0001_initial"
    assert list(migration.operations) == operations
    assert "NOTE: A note." in (tmp_path / "0001_initial.py").read_text()


def test_writer_writes_only_the_arguments_that_are_not_defaults():
    source = migration_source("0002_x", ["0001_initial"], [AddColumn("books", Column("isbn", "string"))])
    assert "migrations.AddColumn('books', migrations.Column('isbn', 'string'))," in source
    assert "default=" not in source


def test_writer_does_not_replace_a_file(tmp_path):
    write_migration(tmp_path, "0001_initial", [], [])
    with pytest.raises(MigrationError, match="exists already"):
        write_migration(tmp_path, "0001_initial", [], [])


# -- Schema editors of PostgreSQL and MySQL ------------------------------------------


def test_postgresql_add_not_null_column_fills_the_rows_first():
    statements = AddColumn("authors", Column("rank", "integer", null=False), default=0).forwards(
        shop_state(), POSTGRESQL
    )
    assert statements == [
        ('ALTER TABLE "authors" ADD COLUMN "rank" INTEGER', []),
        ('UPDATE "authors" SET "rank" = %s', [0]),
        ('ALTER TABLE "authors" ALTER COLUMN "rank" SET NOT NULL', []),
    ]


def test_postgresql_alter_column_type_and_unique():
    new_name = Column("name", "string", max_length=100, null=False, unique=True)
    statements = AlterColumn("authors", new_name).forwards(shop_state(), POSTGRESQL)
    assert statements == [
        ('ALTER TABLE "authors" ALTER COLUMN "name" TYPE VARCHAR(100) USING "name"::VARCHAR(100)', []),
        ('ALTER TABLE "authors" ADD CONSTRAINT "authors_name_key" UNIQUE ("name")', []),
    ]


def test_mysql_drop_column_drops_its_foreign_key_first():
    statements = DropColumn("books", "author_id").forwards(shop_state(), MYSQL)
    assert statements == [
        ("ALTER TABLE `books` DROP FOREIGN KEY `books_author_id_fkey`", []),
        ("ALTER TABLE `books` DROP COLUMN `author_id`", []),
    ]


def test_mysql_alter_column_uses_modify():
    statements = AlterColumn(
        "books", Column("title", "string", max_length=200, null=False), default="x"
    ).forwards(shop_state(), MYSQL)
    assert statements == [
        ("UPDATE `books` SET `title` = %s WHERE `title` IS NULL", ["x"]),
        ("ALTER TABLE `books` MODIFY COLUMN `title` VARCHAR(200) NOT NULL", []),
    ]


def test_renames_keep_the_constraint_names_correct():
    state = SchemaState({"authors": [ID, Column("email", "string", max_length=100, unique=True)]})
    assert RenameColumn("authors", "email", "mail").forwards(state, POSTGRESQL) == [
        ('ALTER TABLE "authors" RENAME COLUMN "email" TO "mail"', []),
        ('ALTER TABLE "authors" RENAME CONSTRAINT "authors_email_key" TO "authors_mail_key"', []),
    ]
    assert RenameTable("authors", "writers").forwards(state, MYSQL) == [
        ("ALTER TABLE `authors` RENAME TO `writers`", []),
        ("ALTER TABLE `writers` RENAME INDEX `authors_email_key` TO `writers_email_key`", []),
    ]


def test_create_table_has_named_constraints_on_postgresql_and_mysql():
    statements = CreateTable(
        "books", [ID, Column("isbn", "string", max_length=13, unique=True), AUTHOR_ID]
    ).forwards(SchemaState(), MYSQL)
    assert statements[0][0] == (
        "CREATE TABLE `books` (\n"
        "    `id` INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,\n"
        "    `isbn` VARCHAR(13),\n"
        "    `author_id` INTEGER,\n"
        "    CONSTRAINT `books_isbn_key` UNIQUE (`isbn`),\n"
        "    CONSTRAINT `books_author_id_fkey` FOREIGN KEY (`author_id`) "
        "REFERENCES `authors` (`id`) ON DELETE CASCADE\n"
        ")"
    )


def test_a_date_default_adds_the_import_of_datetime():
    class Event(Model):
        __tablename__ = "events"
        day = DateField(default=datetime.date(2022, 1, 1), null=False)

    changes = detect_changes(SchemaState({"events": [ID]}), SchemaState.from_models([Event]), [Event])
    source = migration_source("0002_x", ["0001_initial"], changes.operations)
    assert "import datetime\n" in source
    assert "default=datetime.date(2022, 1, 1)" in source
