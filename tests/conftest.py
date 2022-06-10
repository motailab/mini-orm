"""Shared fixtures for the tests.

The tests that use a database run once for each available database. SQLite
is always available. To run the tests on PostgreSQL and MySQL too, set the
URL of an empty test database in these environment variables:

    MINI_ORM_POSTGRESQL_URL=postgresql://postgres:mini_orm@127.0.0.1:55432/mini_orm
    MINI_ORM_MYSQL_URL=mysql://root:mini_orm@127.0.0.1:53306/mini_orm

The tests drop ALL tables of these databases. Use a database only for the tests.
"""

import os

import pytest

from mini_orm import Database, Session

from .example_models import ALL_MODELS, Author, Book

DATABASE_URLS = {
    "sqlite": ":memory:",
    "postgresql": os.environ.get("MINI_ORM_POSTGRESQL_URL"),
    "mysql": os.environ.get("MINI_ORM_MYSQL_URL"),
}
BACKENDS = [name for name, url in DATABASE_URLS.items() if url]


class RecordingDatabase(Database):
    """A Database that records each SQL statement, so a test can count queries."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return super().execute(sql, params)

    def selects(self) -> list[str]:
        """Return the SELECT statements that ran since the last clear()."""
        return [sql for sql in self.statements if sql.startswith("SELECT")]

    def clear(self) -> None:
        self.statements = []


def drop_all_tables(database: Database) -> None:
    """Drop each table of the database, also the tables of earlier test runs."""
    dialect = database.dialect
    if dialect.name == "mysql":
        database.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in database.table_names():
        cascade = " CASCADE" if dialect.name == "postgresql" else ""
        database.execute(f"DROP TABLE IF EXISTS {dialect.quote_name(table)}{cascade}")
    if dialect.name == "mysql":
        database.execute("SET FOREIGN_KEY_CHECKS = 1")


def dialect_sql(database: Database, sql: str) -> str:
    """Write a statement of the SQLite style for the dialect of the database.

    The tests write SQL with the double quotes and the "?" of SQLite.
    PostgreSQL uses "%s", and MySQL uses backticks and "%s".
    """
    return sql.replace('"', database.dialect.quote_character).replace("?", database.dialect.placeholder)


@pytest.fixture(params=BACKENDS)
def empty_db(request):
    """An empty database. The test runs once for each available database."""
    database = RecordingDatabase(DATABASE_URLS[request.param])
    drop_all_tables(database)
    database.clear()
    yield database
    drop_all_tables(database)
    database.close()


@pytest.fixture
def db(empty_db):
    """A database with the tables of the example models."""
    empty_db.create_tables(ALL_MODELS)
    empty_db.clear()
    return empty_db


@pytest.fixture
def session(db):
    with Session(db) as session:
        yield session


@pytest.fixture
def shop(db):
    """Put two authors and three books in the database. Return nothing.

    The tests open their own sessions, so no object comes from this
    fixture's session.
    """
    with Session(db) as session:
        alice = Author(name="Alice", email="alice@example.com")
        bob = Author(name="Bob", email="bob@example.com")
        session.add_all([alice, bob])
        session.add_all(
            [
                Book(title="SQL Basics", price=20.0, published=True, author=alice),
                Book(title="Advanced SQL", price=35.0, published=True, author=alice),
                Book(title="Python Tricks", price=25.0, published=False, author=bob),
            ]
        )
        session.commit()
    db.clear()
