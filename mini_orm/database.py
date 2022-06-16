"""The Database class: one connection to SQLite, PostgreSQL or MySQL."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from .dialects import dialect_for_url
from .exceptions import DatabaseError, IntegrityError
from .schema import create_table_sql, drop_table_sql, sort_models

if TYPE_CHECKING:
    from .models import Model


class Database:
    """A connection to a database.

    * url: the database. The URL selects the dialect:

      - ``":memory:"`` or ``"shop.db"`` (an SQLite file), or ``"sqlite:///shop.db"``
      - ``"postgresql://user:password@localhost:5432/shop"``
      - ``"mysql://user:password@localhost:3306/shop"``

    * echo: if True, print each SQL statement and its parameters. Use it to
      see the SQL that the ORM makes.

    Use one Session at a time with a Database. The sessions share the
    connection, and a connection has only one transaction at a time.
    """

    def __init__(self, url: str = ":memory:", *, echo: bool = False) -> None:
        self.url = url
        self.echo = echo
        self.dialect = dialect_for_url(url)
        self.driver = self.dialect.driver()
        # The dialect opens the connection in autocommit mode. So the driver
        # does not start transactions by itself. The Session controls the
        # transactions with BEGIN, COMMIT and ROLLBACK.
        self.connection = self.dialect.connect(url)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one SQL statement and return the cursor.

        The method changes each error of the driver into DatabaseError or
        IntegrityError, so your code catches the same error for each database.
        """
        if self.echo:
            print(f"SQL: {sql}" + (f"  PARAMS: {list(params)}" if params else ""))
        cursor = self.connection.cursor()
        try:
            if params:
                cursor.execute(sql, [self.dialect.adapt_value(value) for value in params])
            else:
                # Without parameters, a driver with the "%s" style does not
                # read the SQL for placeholders, so a "%" in the text is safe.
                cursor.execute(sql)
        except self.driver.IntegrityError as error:
            raise IntegrityError(str(error)) from error
        except self.driver.Error as error:
            raise DatabaseError(str(error)) from error
        return cursor

    # -- Transactions ---------------------------------------------------------

    def begin(self) -> None:
        self.execute("BEGIN")

    def commit(self) -> None:
        self.execute("COMMIT")

    def rollback(self) -> None:
        self.execute("ROLLBACK")

    @property
    def in_transaction(self) -> bool:
        """True if a transaction is open on the connection."""
        return self.dialect.in_transaction(self.connection)

    # -- Schema ---------------------------------------------------------------

    def create_tables(self, models: Iterable[type[Model]]) -> None:
        """Create the tables of the models. Skip the tables that exist."""
        for model in sort_models(models):
            self.execute(create_table_sql(model, self.dialect))

    def drop_tables(self, models: Iterable[type[Model]]) -> None:
        """Drop the tables of the models. Skip the tables that do not exist."""
        for model in reversed(sort_models(models)):
            self.execute(drop_table_sql(model, self.dialect))

    def table_names(self) -> list[str]:
        """Return the names of the tables in the database, in alphabetical order."""
        return [row[0] for row in self.execute(self.dialect.table_names_sql()).fetchall()]

    def column_names(self, table: str) -> list[str]:
        """Return the names of the columns of a table, in column order."""
        return [row[0] for row in self.execute(self.dialect.column_names_sql(), [table]).fetchall()]

    # -- Connection -----------------------------------------------------------

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
