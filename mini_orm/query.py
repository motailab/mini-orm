"""The Query class: a builder for SELECT statements.

A Query object describes a SELECT statement. It does not run SQL when you
make it or change it. It runs SQL only when you ask for results, for example
with all(), first() or count().

A Query is immutable. Each builder method (where, order_by, limit, ...)
returns a new Query and does not change the old one:

    adults = session.query(User).where(User.age >= 18)
    young_adults = adults.where(User.age < 30)   # "adults" does not change
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, NamedTuple

from .compiler import compile_count, compile_delete, compile_exists, compile_select, compile_update
from .dialects import SQLITE, Dialect
from .exceptions import DoesNotExist, MultipleObjectsReturned, QueryError, SessionError
from .expressions import SQL, And, Condition, Operand, Ordering
from .fields import Field
from .relations import ForeignKey, ReverseRelation

if TYPE_CHECKING:
    from .models import Model
    from .session import Session


class Join(NamedTuple):
    """One JOIN clause of a query."""

    model: type[Model]
    condition: Condition
    kind: str  # "INNER" or "LEFT"


class Query:
    """A SELECT statement for one model.

    Make a query with ``session.query(Model)``. A query without a session
    (``Query(Model)``) can make SQL text, but it cannot run.
    """

    def __init__(self, model: type[Model], session: Session | None = None) -> None:
        if getattr(model, "_meta", None) is None or model._meta.abstract:
            raise QueryError(f"Query needs a model class with a table, not {model!r}.")
        self.model = model
        self.session = session
        # All attributes hold immutable values (None, tuples, expression
        # nodes), so a shallow copy of the query is a safe copy.
        self._where: Condition | None = None
        self._joins: tuple[Join, ...] = ()
        self._order_by: tuple[Ordering, ...] = ()
        self._limit: int | None = None
        self._offset: int | None = None
        self._distinct = False
        self._group_by: tuple[Field, ...] = ()
        self._having: Condition | None = None
        # Values mode: (key, expression) pairs. None means "return instances".
        self._values: tuple[tuple[str, Operand], ...] | None = None
        self._select_related: tuple[ForeignKey, ...] = ()
        self._prefetch_related: tuple[ForeignKey | ReverseRelation, ...] = ()

    def _clone(self, **changes: Any) -> Query:
        """Return a copy of the query with some attributes changed."""
        clone = copy.copy(self)
        for attribute, value in changes.items():
            setattr(clone, attribute, value)
        return clone

    # -- Builder methods: each returns a new Query ----------------------------

    def where(self, *conditions: Condition) -> Query:
        """Add conditions to the WHERE clause. All conditions must be true (AND)."""
        return self._clone(_where=_combine(self._where, conditions, "where"))

    def join(self, model: type[Model], on: Condition | None = None) -> Query:
        """Add an INNER JOIN with another model.

        Without ``on``, the query finds the ForeignKey between the model and
        a model that is already in the query. The join can go in the two
        directions: Book -> Author (many-to-one) and Author -> Book
        (one-to-many). Use the columns of the joined model in where().
        """
        self._check_not_in_query(model)
        if on is None:
            on = self._find_join_condition(model)
        elif not isinstance(on, Condition):
            raise QueryError(f"join() needs a condition for on=, not {on!r}.")
        return self._clone(_joins=self._joins + (Join(model, on, "INNER"),))

    def select_related(self, *names: str) -> Query:
        """Load related objects in the same SELECT, with a LEFT JOIN.

        Each name must be a ForeignKey of the query model. This prevents the
        "N+1 queries" problem: one query loads the books and their authors.
        """
        self._check_not_values_mode("select_related")
        joins = self._joins
        foreign_keys = self._select_related
        for name in names:
            field = self.model._meta.get_field(name)
            if not isinstance(field, ForeignKey):
                raise QueryError(f"select_related() needs a ForeignKey, and {field.label} is not one.")
            target = field.target_model
            if target is self.model:
                raise QueryError(
                    f"select_related({name!r}) needs a join of {target.__name__} with "
                    "itself, which needs table aliases. mini_orm does not support "
                    "aliases. Use prefetch_related() instead."
                )
            self._check_not_in_query(target, joins)
            condition = field == target._meta.primary_key
            joins = joins + (Join(target, condition, "LEFT"),)
            foreign_keys = foreign_keys + (field,)
        return self._clone(_joins=joins, _select_related=foreign_keys)

    def prefetch_related(self, *names: str) -> Query:
        """Load related objects with one more SELECT for each name.

        A name can be a ForeignKey (book -> author) or a reverse relation
        (author -> books). After the main query, the ORM runs
        ``SELECT ... WHERE key IN (...)`` once for each name.
        """
        self._check_not_values_mode("prefetch_related")
        relations = tuple(self.model._meta.get_relation(name) for name in names)
        return self._clone(_prefetch_related=self._prefetch_related + relations)

    def order_by(self, *items: Field | Ordering | str) -> Query:
        """Set the ORDER BY clause. This replaces an earlier order_by().

        An item can be a field (``User.name``), an ordering
        (``User.name.desc()``) or a field name (``"name"`` or ``"-name"``,
        where "-" means descending order).
        """
        orderings = tuple(self._to_ordering(item) for item in items)
        return self._clone(_order_by=orderings)

    def limit(self, count: int) -> Query:
        """Return at most ``count`` rows."""
        return self._clone(_limit=_check_count("limit", count))

    def offset(self, count: int) -> Query:
        """Skip the first ``count`` rows."""
        return self._clone(_offset=_check_count("offset", count))

    def distinct(self) -> Query:
        """Remove duplicate rows (SELECT DISTINCT)."""
        return self._clone(_distinct=True)

    def group_by(self, *items: Field | str) -> Query:
        """Add columns to the GROUP BY clause. Use it with values() and aggregates."""
        fields = tuple(self._to_field(item) for item in items)
        return self._clone(_group_by=self._group_by + fields)

    def having(self, *conditions: Condition) -> Query:
        """Add conditions to the HAVING clause, for example ``Count() > 1``."""
        return self._clone(_having=_combine(self._having, conditions, "having"))

    def values(self, *fields: Field | str, **expressions: Operand) -> Query:
        """Return dictionaries instead of model instances.

        Give fields (the key is the field name) and named expressions (the
        key is the argument name). Without arguments, the query returns all
        fields of the model.

            query.group_by(Book.author).values(Book.author, total=Count())
            # [{"author": 1, "total": 3}, ...]
        """
        if self._select_related or self._prefetch_related:
            raise QueryError("values() cannot be used with select_related() or prefetch_related().")
        columns: list[tuple[str, Operand]] = []
        for item in fields:
            field = self._to_field(item)
            columns.append((field.name, field))
        for key, expression in expressions.items():
            if not isinstance(expression, Operand):
                raise QueryError(f"values() needs a field or an aggregate for {key!r}, not {expression!r}.")
            columns.append((key, expression))
        if not columns:
            columns = list(self.model._meta.fields.items())
        keys = [key for key, _ in columns]
        if len(keys) != len(set(keys)):
            raise QueryError(f"values() has the same key two times: {keys}")
        return self._clone(_values=tuple(columns))

    # -- Methods that run SQL -------------------------------------------------

    def all(self) -> list[Any]:
        """Run the query. Return a list of model instances (or dicts in values mode)."""
        session = self._require_session()
        sql, params = self.to_sql()
        rows = session.execute(sql, params).fetchall()
        if self._values is not None:
            return [self._row_to_dict(row) for row in rows]
        instances = [self._row_to_instance(row, session) for row in rows]
        for relation in self._prefetch_related:
            relation.prefetch(instances, session)
        return instances

    def __iter__(self) -> Iterator[Any]:
        return iter(self.all())

    def first(self) -> Any:
        """Return the first result, or None if there is no result."""
        results = self.limit(1).all()
        return results[0] if results else None

    def one(self) -> Any:
        """Return the only result. Raise an error if there is no result or more than one."""
        # LIMIT 2 is sufficient to know if there is more than one row.
        results = self.limit(2).all()
        if not results:
            raise DoesNotExist(f"No {self.model.__name__} matches the query.")
        if len(results) > 1:
            raise MultipleObjectsReturned(f"More than one {self.model.__name__} matches the query.")
        return results[0]

    def count(self) -> int:
        """Return the number of rows that the query finds."""
        session = self._require_session()
        sql, params = compile_count(self._without_related(), session.database.dialect)
        return session.execute(sql, params).fetchone()[0]

    def exists(self) -> bool:
        """Return True if the query finds at least one row."""
        session = self._require_session()
        sql, params = compile_exists(self._without_related(), session.database.dialect)
        return bool(session.execute(sql, params).fetchone()[0])

    def aggregate(self, **expressions: Operand) -> dict[str, Any]:
        """Calculate aggregate values for all rows of the query. Return one dict.

        Example:

            session.query(Book).aggregate(total=Count(), average=Avg(Book.price))
            # {"total": 3, "average": 21.5}
        """
        if not expressions:
            raise QueryError("aggregate() needs at least one named expression.")
        if self._group_by or self._limit is not None or self._offset is not None or self._distinct:
            raise QueryError(
                "aggregate() cannot be used with group_by(), limit(), offset() or distinct(). "
                "Use group_by() with values() to get one result for each group."
            )
        query = self._without_related()._clone(_order_by=())
        return query.values(**expressions).all()[0]

    def update(self, **values: Any) -> int:
        """Change the rows that match the WHERE clause. Return the number of rows.

        This runs one UPDATE statement. It does not load the rows, and it
        does not change instances that the session already has in memory.
        """
        self._check_simple_statement("update")
        if not values:
            raise QueryError("update() needs at least one field and value.")
        assignments = []
        for field_name, value in values.items():
            field = self.model._meta.get_field(field_name)
            if field.primary_key:
                raise QueryError("update() cannot change a primary key.")
            assignments.append((field, field.to_db(field.validate(value))))
        session = self._require_session()
        sql, params = compile_update(self, assignments, session.database.dialect)
        return session.execute(sql, params).rowcount

    def delete(self) -> int:
        """Delete the rows that match the WHERE clause. Return the number of rows.

        This runs one DELETE statement. It does not remove instances that
        the session already has in memory.
        """
        self._check_simple_statement("delete")
        session = self._require_session()
        sql, params = compile_delete(self, session.database.dialect)
        return session.execute(sql, params).rowcount

    def to_sql(self, dialect: Dialect | None = None) -> SQL:
        """Return the SELECT statement and its parameters. Do not run it.

        Without a dialect, the query uses the dialect of its session. A query
        without a session uses SQLite. Give a dialect to see the SQL for a
        different database, for example ``query.to_sql(PostgreSQLDialect())``.
        """
        if dialect is None:
            dialect = self.session.database.dialect if self.session is not None else SQLITE
        return compile_select(self, dialect)

    def __repr__(self) -> str:
        sql, params = self.to_sql()
        return f"<Query {self.model.__name__}: {sql} {params}>"

    # -- Helpers --------------------------------------------------------------

    def _require_session(self) -> Session:
        if self.session is None:
            raise SessionError("The query has no session. Use session.query(Model) to make it.")
        return self.session

    def _without_related(self) -> Query:
        """Return a copy that does not load related objects.

        count(), exists() and aggregate() do not make instances, so they
        do not need the columns of the related models. The LEFT JOINs stay,
        because where() can use the columns of a joined model. A LEFT JOIN
        on a many-to-one relationship does not change the number of rows.
        """
        return self._clone(_select_related=(), _prefetch_related=())

    def _row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        assert self._values is not None
        pairs = zip(self._values, row, strict=True)
        return {key: expression.from_db(value) for (key, expression), value in pairs}

    def _row_to_instance(self, row: tuple[Any, ...], session: Session) -> Model:
        """Make the instances from one row of the SELECT.

        The row has the columns of the query model first. Then it has the
        columns of each select_related model, in the order of the names.
        """
        field_count = len(self.model._meta.fields)
        instance = session._load_row(self.model, row[:field_count])
        assert instance is not None, "A row of the query model always has a primary key."
        start = field_count
        for foreign_key in self._select_related:
            target = foreign_key.target_model
            end = start + len(target._meta.fields)
            instance._state.related[foreign_key.name] = session._load_row(target, row[start:end])
            start = end
        return instance

    def _models_in_query(self, joins: tuple[Join, ...] | None = None) -> list[type[Model]]:
        joins = self._joins if joins is None else joins
        return [self.model] + [join.model for join in joins]

    def _check_not_in_query(self, model: type[Model], joins: tuple[Join, ...] | None = None) -> None:
        if model in self._models_in_query(joins):
            raise QueryError(
                f"{model.__name__} is already in the query. A second join of the "
                "same table needs table aliases, which mini_orm does not support."
            )

    def _find_join_condition(self, target: type[Model]) -> Condition:
        """Find the ForeignKey between the target and a model in the query."""
        conditions = []
        for model in self._models_in_query():
            for foreign_key in model._meta.foreign_keys:
                if foreign_key.target_model is target:
                    conditions.append(foreign_key == target._meta.primary_key)
            for foreign_key in target._meta.foreign_keys:
                if foreign_key.target_model is model:
                    conditions.append(foreign_key == model._meta.primary_key)
        if not conditions:
            raise QueryError(f"No ForeignKey connects {target.__name__} with the models in the query.")
        if len(conditions) > 1:
            raise QueryError(
                f"More than one ForeignKey connects {target.__name__} with the models "
                "in the query. Give the condition with join(model, on=...)."
            )
        return conditions[0]

    def _to_field(self, item: Field | str) -> Field:
        if isinstance(item, str):
            return self.model._meta.get_field(item)
        if isinstance(item, Field):
            return item
        raise QueryError(f"Expected a field or a field name, not {item!r}.")

    def _to_ordering(self, item: Field | Ordering | str) -> Ordering:
        if isinstance(item, Ordering):
            return item
        if isinstance(item, str) and item.startswith("-"):
            return self._to_field(item[1:]).desc()
        return self._to_field(item).asc()

    def _check_not_values_mode(self, method: str) -> None:
        if self._values is not None:
            raise QueryError(f"{method}() cannot be used with values(). Call {method}() first.")

    def _check_simple_statement(self, method: str) -> None:
        """update() and delete() support only a WHERE clause."""
        if (
            self._joins
            or self._group_by
            or self._having is not None
            or self._order_by
            or self._limit is not None
            or self._offset is not None
            or self._distinct
            or self._values is not None
        ):
            raise QueryError(f"{method}() supports only where(). Remove the other clauses.")


def _combine(current: Condition | None, conditions: tuple[Condition, ...], method: str) -> Condition | None:
    """Join the current condition and the new conditions with AND."""
    combined = current
    for condition in conditions:
        if not isinstance(condition, Condition):
            raise QueryError(f"{method}() needs conditions such as User.age > 18, not {condition!r}.")
        combined = condition if combined is None else And(combined, condition)
    return combined


def _check_count(method: str, count: int) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise QueryError(f"{method}() needs an int, not {type(count).__name__}.")
    if count < 0:
        raise QueryError(f"{method}() needs a number that is 0 or more, not {count}.")
    return count
