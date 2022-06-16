"""Dialects: the differences between SQLite, PostgreSQL and MySQL.

A dialect knows how one database writes SQL and how its Python driver
works. The rest of the ORM asks the dialect and does not know the database:

* the placeholder of a parameter: ``?`` (SQLite) or ``%s`` (PostgreSQL, MySQL)
* the quotes of a name: ``"name"`` (SQLite, PostgreSQL) or ```name``` (MySQL)
* the SQL type of each column type, and the auto-increment primary key
* LIMIT and OFFSET, the INSERT of a row without values, and the new primary key
* the connection, the state of the transaction and the introspection queries

The three drivers follow the Python database API (PEP 249): connect(),
cursor(), execute(), fetchall(), rowcount, and the same exception classes.
"""

from __future__ import annotations

import datetime
import importlib
from collections.abc import Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from .exceptions import ModelDefinitionError, ORMError

if TYPE_CHECKING:
    from .expressions import SQL
    from .schema import Column

# PostgreSQL cuts longer names. MySQL refuses names longer than 64 characters.
MAX_NAME_LENGTH = 63


class Dialect:
    """The base class. It has the behavior that most databases share.

    A subclass changes only the parts that are different. Most of the
    settings are class attributes.
    """

    name = ""
    driver_module = ""  # The import name of the Python driver.
    install_hint = ""  # The pip command that installs the driver.
    placeholder = "%s"
    quote_character = '"'
    # The SQL type of each column type. A string with max_length is VARCHAR(n).
    type_names: dict[str, str] = {}
    # The LIMIT text for a query that has an OFFSET but no LIMIT. None means
    # that the database accepts OFFSET without LIMIT.
    no_limit: str | None = None

    # -- Connection -----------------------------------------------------------

    def driver(self) -> ModuleType:
        """Import the Python driver. Import it only when necessary, so a user
        of SQLite does not need the drivers of the other databases."""
        try:
            return importlib.import_module(self.driver_module)
        except ImportError:
            raise ORMError(
                f"{self.name} needs the {self.driver_module} package: {self.install_hint}"
            ) from None

    def connect(self, url: str) -> Any:
        """Open a connection in autocommit mode. The session sends BEGIN and COMMIT itself."""
        raise NotImplementedError

    def in_transaction(self, connection: Any) -> bool:
        """Return True if the connection has an open transaction."""
        raise NotImplementedError

    # -- Names and values -----------------------------------------------------

    def quote_name(self, name: str) -> str:
        """Put a table name or a column name in quotes.

        A parameter can hold only a value, not a name, so the ORM writes
        names into the SQL text. The quotes make each name safe, also a
        reserved word such as "order". A quote character inside the name
        becomes two quote characters, which is the SQL escape for it.
        """
        quote = self.quote_character
        return f"{quote}{name.replace(quote, quote + quote)}{quote}"

    def adapt_value(self, value: Any) -> Any:
        """Change a parameter value into a value that the driver accepts."""
        return value

    # -- Column types and CREATE TABLE ----------------------------------------

    def column_type(self, column: Column) -> str:
        """Return the SQL type of a column."""
        if column.type == "string" and column.max_length is not None:
            return f"VARCHAR({column.max_length})"
        return self.type_names[column.type]

    def column_definition(self, column: Column) -> str:
        """Return the definition of one column, for example ``"title" TEXT NOT NULL``."""
        parts = [self.quote_name(column.name), self.column_type(column)]
        if column.primary_key:
            parts.append(self.primary_key_sql(column))
        elif not column.null:
            parts.append("NOT NULL")
        return " ".join(parts)

    def primary_key_sql(self, column: Column) -> str:
        return "PRIMARY KEY"

    def table_constraints(self, table: str, columns: Sequence[Column]) -> list[str]:
        """Return the UNIQUE and FOREIGN KEY constraints of a table, with names.

        A migration can drop a constraint only if it knows its name. So the
        ORM gives each constraint a name, as PostgreSQL does by default:
        "books_isbn_key" for UNIQUE and "books_author_id_fkey" for a foreign key.
        """
        constraints = []
        for column in columns:
            if column.unique and not column.primary_key:
                name = self.quote_name(self.unique_name(table, column.name))
                constraints.append(f"CONSTRAINT {name} UNIQUE ({self.quote_name(column.name)})")
        for column in columns:
            if column.references is not None:
                name = self.quote_name(self.foreign_key_name(table, column.name))
                constraints.append(f"CONSTRAINT {name} {self.foreign_key_sql(column)}")
        return constraints

    def foreign_key_sql(self, column: Column) -> str:
        assert column.references is not None
        target_table, target_column = column.references
        sql = (
            f"FOREIGN KEY ({self.quote_name(column.name)}) "
            f"REFERENCES {self.quote_name(target_table)} ({self.quote_name(target_column)})"
        )
        if column.on_delete is not None:
            sql += f" ON DELETE {column.on_delete}"
        return sql

    def unique_name(self, table: str, column: str) -> str:
        return f"{table}_{column}_key"[:MAX_NAME_LENGTH]

    def foreign_key_name(self, table: str, column: str) -> str:
        return f"{table}_{column}_fkey"[:MAX_NAME_LENGTH]

    def create_table_sql(self, table: str, columns: Sequence[Column], *, if_not_exists: bool = True) -> str:
        lines = [self.column_definition(column) for column in columns]
        lines += self.table_constraints(table, columns)
        body = ",\n    ".join(lines)
        exists = "IF NOT EXISTS " if if_not_exists else ""
        return f"CREATE TABLE {exists}{self.quote_name(table)} (\n    {body}\n)"

    def drop_table_sql(self, table: str) -> str:
        return f"DROP TABLE IF EXISTS {self.quote_name(table)}"

    # -- Queries --------------------------------------------------------------

    def limit_offset_sql(self, limit: int | None, offset: int | None) -> SQL:
        parts: list[str] = []
        params: list[Any] = []
        if limit is not None:
            parts.append(f"LIMIT {self.placeholder}")
            params.append(limit)
        elif offset is not None and self.no_limit is not None:
            parts.append(self.no_limit)
        if offset is not None:
            parts.append(f"OFFSET {self.placeholder}")
            params.append(offset)
        return " ".join(parts), params

    def empty_insert_sql(self, table: str) -> str:
        """Return the INSERT statement for a row that has no values, only defaults."""
        return f"INSERT INTO {self.quote_name(table)} DEFAULT VALUES"

    def returning_sql(self, column: str) -> str:
        """Return the text that an INSERT needs to give the new primary key."""
        return ""

    def new_primary_key(self, cursor: Any) -> Any:
        """Return the primary key that the database made for the last INSERT."""
        return cursor.lastrowid

    # -- Introspection --------------------------------------------------------

    def table_names_sql(self) -> str:
        raise NotImplementedError

    def column_names_sql(self) -> str:
        """Return a query with one placeholder for the table name."""
        raise NotImplementedError


class SQLiteDialect(Dialect):
    """SQLite: a database in one file, with the sqlite3 module of Python."""

    name = "sqlite"
    driver_module = "sqlite3"
    placeholder = "?"
    type_names = {
        "integer": "INTEGER",
        "float": "REAL",
        "string": "TEXT",
        # SQLite has no boolean type and no date type.
        "boolean": "INTEGER",
        "datetime": "TEXT",
        "date": "TEXT",
    }
    no_limit = "LIMIT -1"

    def connect(self, url: str) -> Any:
        sqlite3 = self.driver()
        # isolation_level=None: the sqlite3 module does not start transactions by itself.
        connection = sqlite3.connect(_sqlite_path(url), isolation_level=None)
        # SQLite does not check foreign keys unless this setting is on, for each connection.
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def in_transaction(self, connection: Any) -> bool:
        return bool(connection.in_transaction)

    def adapt_value(self, value: Any) -> Any:
        # SQLite stores a date as ISO 8601 text, which sorts in time order.
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        return value

    def primary_key_sql(self, column: Column) -> str:
        # An INTEGER PRIMARY KEY is an alias of the row ID: SQLite gives each
        # new row the next number. NOT NULL prevents an old SQLite bug: it
        # accepts NULL in a primary key column that is not INTEGER.
        return "PRIMARY KEY NOT NULL"

    def column_definition(self, column: Column) -> str:
        definition = super().column_definition(column)
        if column.unique and not column.primary_key:
            definition += " UNIQUE"
        return definition

    def table_constraints(self, table: str, columns: Sequence[Column]) -> list[str]:
        # SQLite changes a table with a table remake, so it never needs the
        # name of a constraint. UNIQUE is part of the column definition.
        return [self.foreign_key_sql(column) for column in columns if column.references is not None]

    def table_names_sql(self) -> str:
        return (
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )

    def column_names_sql(self) -> str:
        return "SELECT name FROM pragma_table_info(?) ORDER BY cid"


class PostgreSQLDialect(Dialect):
    """PostgreSQL, with the psycopg driver (version 3)."""

    name = "postgresql"
    driver_module = "psycopg"
    install_hint = "pip install 'psycopg[binary]'"
    type_names = {
        "integer": "INTEGER",
        "float": "DOUBLE PRECISION",
        "string": "TEXT",
        "boolean": "BOOLEAN",
        "datetime": "TIMESTAMP",
        "date": "DATE",
    }

    def connect(self, url: str) -> Any:
        return self.driver().connect(url, autocommit=True)

    def in_transaction(self, connection: Any) -> bool:
        status = connection.info.transaction_status
        return status != self.driver().pq.TransactionStatus.IDLE

    def primary_key_sql(self, column: Column) -> str:
        if column.type == "integer":
            # BY DEFAULT accepts an explicit value too, not only a generated one.
            return "GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        return "PRIMARY KEY"

    def returning_sql(self, column: str) -> str:
        # psycopg has no lastrowid, so the INSERT returns the new key itself.
        return f" RETURNING {self.quote_name(column)}"

    def new_primary_key(self, cursor: Any) -> Any:
        return cursor.fetchone()[0]

    def table_names_sql(self) -> str:
        return "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"

    def column_names_sql(self) -> str:
        return (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position"
        )


class MySQLDialect(Dialect):
    """MySQL, with the PyMySQL driver."""

    name = "mysql"
    driver_module = "pymysql"
    install_hint = "pip install 'PyMySQL[rsa]'"
    quote_character = "`"
    type_names = {
        "integer": "INTEGER",
        "float": "DOUBLE",
        "string": "TEXT",
        "boolean": "BOOLEAN",
        # (6) keeps the microseconds.
        "datetime": "DATETIME(6)",
        "date": "DATE",
    }
    # MySQL needs a LIMIT before OFFSET. Its manual gives this large number.
    no_limit = "LIMIT 18446744073709551615"

    def connect(self, url: str) -> Any:
        pymysql = self.driver()
        parts = urlsplit(url)
        return pymysql.connect(
            host=parts.hostname or "localhost",
            port=parts.port or 3306,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=parts.path.lstrip("/") or None,
            charset="utf8mb4",
            autocommit=True,
            # Without FOUND_ROWS, rowcount counts only the rows whose values
            # changed. An UPDATE with the same values gives 0, and the session
            # thinks that the row is gone.
            client_flag=pymysql.constants.CLIENT.FOUND_ROWS,
        )

    def in_transaction(self, connection: Any) -> bool:
        in_trans = self.driver().constants.SERVER_STATUS.SERVER_STATUS_IN_TRANS
        return bool(connection.server_status & in_trans)

    def column_type(self, column: Column) -> str:
        if column.type == "string" and column.max_length is None and (column.unique or column.primary_key):
            raise ModelDefinitionError(
                f"MySQL cannot put a UNIQUE or PRIMARY KEY constraint on the TEXT column "
                f"{column.name!r}. Give the StringField a max_length."
            )
        return super().column_type(column)

    def primary_key_sql(self, column: Column) -> str:
        if column.type == "integer":
            return "NOT NULL AUTO_INCREMENT PRIMARY KEY"
        return "NOT NULL PRIMARY KEY"

    def empty_insert_sql(self, table: str) -> str:
        return f"INSERT INTO {self.quote_name(table)} () VALUES ()"

    def table_names_sql(self) -> str:
        return (
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        )

    def column_names_sql(self) -> str:
        return (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
        )


# The dialect of a query that has no session, for example Query(Book).to_sql().
SQLITE = SQLiteDialect()


def dialect_for_url(url: str) -> Dialect:
    """Return the dialect of a database URL or of an SQLite file name.

    * "postgresql://user:password@host:5432/name" (also "postgres://")
    * "mysql://user:password@host:3306/name"
    * "sqlite:///shop.db", "shop.db" or ":memory:"
    """
    if url.startswith(("postgresql://", "postgres://")):
        return PostgreSQLDialect()
    if url.startswith("mysql://"):
        return MySQLDialect()
    return SQLiteDialect()


def _sqlite_path(url: str) -> str:
    """Return the file name of an SQLite URL: "sqlite:///shop.db" -> "shop.db"."""
    if url == "sqlite://":
        return ":memory:"
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    return url
