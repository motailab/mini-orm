"""The SQL compiler: it makes SQL statements from queries and instances.

Each function returns a pair: the SQL text and the list of parameters.
No function in this module runs SQL. The Session runs the SQL.

The expression nodes (expressions.py) make the small parts, for example
one condition. This module puts the parts together in the correct order:

    SELECT ... FROM ... JOIN ... WHERE ... GROUP BY ... HAVING ...
    ORDER BY ... LIMIT ... OFFSET ...

The order of the parameters must be the same as the order of the
placeholders in the text. So each function adds the parameters of a
clause at the same time as it adds the text of the clause.

Each function gets the dialect of the database. The dialect gives the
placeholders, the quotes of the names and the parts that are different in
each database, for example LIMIT and OFFSET.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .expressions import SQL, Expression

if TYPE_CHECKING:
    from .dialects import Dialect
    from .fields import Field
    from .models import Model
    from .query import Query


def compile_select(query: Query, dialect: Dialect) -> SQL:
    """Make the SELECT statement of a query."""
    parts: list[str] = []
    params: list[Any] = []

    columns_sql, columns_params = _join_expressions(_select_columns(query), dialect)
    distinct = "DISTINCT " if query._distinct else ""
    parts.append(f"SELECT {distinct}{columns_sql}")
    params += columns_params

    parts.append(f"FROM {dialect.quote_name(query.model._meta.table_name)}")

    for join in query._joins:
        condition_sql, condition_params = join.condition.to_sql(dialect)
        parts.append(f"{join.kind} JOIN {dialect.quote_name(join.model._meta.table_name)} ON {condition_sql}")
        params += condition_params

    if query._where is not None:
        where_sql, where_params = query._where.to_sql(dialect)
        parts.append(f"WHERE {where_sql}")
        params += where_params

    if query._group_by:
        group_sql, group_params = _join_expressions(query._group_by, dialect)
        parts.append(f"GROUP BY {group_sql}")
        params += group_params

    if query._having is not None:
        having_sql, having_params = query._having.to_sql(dialect)
        parts.append(f"HAVING {having_sql}")
        params += having_params

    if query._order_by:
        order_sql, order_params = _join_expressions(query._order_by, dialect)
        parts.append(f"ORDER BY {order_sql}")
        params += order_params

    limit_sql, limit_params = dialect.limit_offset_sql(query._limit, query._offset)
    if limit_sql:
        parts.append(limit_sql)
        params += limit_params

    return " ".join(parts), params


def compile_count(query: Query, dialect: Dialect) -> SQL:
    """Make a statement that counts the rows of a query.

    The query goes into a subquery. Then the count is correct also for a
    query with LIMIT, DISTINCT or GROUP BY. For example, a count of a
    query with GROUP BY gives the number of groups.
    """
    select_sql, params = compile_select(query, dialect)
    return f"SELECT COUNT(*) FROM ({select_sql}) AS {dialect.quote_name('subquery')}", params


def compile_exists(query: Query, dialect: Dialect) -> SQL:
    """Make a statement that gives true (or 1) if the query finds a row."""
    select_sql, params = compile_select(query, dialect)
    return f"SELECT EXISTS ({select_sql})", params


def compile_insert(instance: Model, dialect: Dialect) -> SQL:
    """Make the INSERT statement for one instance.

    If the primary key is None, the statement does not include it, so the
    database makes the value. For PostgreSQL, the statement then ends with
    RETURNING, because its driver cannot give the new key in another way.
    """
    meta = instance._meta
    fields = [field for field in meta.fields.values() if not (field.primary_key and instance.pk is None)]
    if fields:
        table = dialect.quote_name(meta.table_name)
        columns = ", ".join(dialect.quote_name(field.column) for field in fields)
        placeholders = ", ".join(dialect.placeholder for _ in fields)
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
    else:
        sql = dialect.empty_insert_sql(meta.table_name)
    if instance.pk is None:
        sql += dialect.returning_sql(meta.primary_key.column)
    params = [field.to_db(instance.__dict__.get(field.name)) for field in fields]
    return sql, params


def compile_update(query: Query, assignments: list[tuple[Field, Any]], dialect: Dialect) -> SQL:
    """Make an UPDATE statement for the rows of a query.

    ``assignments`` is a list of (field, value) pairs. Each value must be
    a database value already (the result of ``field.to_db()``).
    """
    table = dialect.quote_name(query.model._meta.table_name)
    set_sql = ", ".join(
        f"{dialect.quote_name(field.column)} = {dialect.placeholder}" for field, _ in assignments
    )
    params = [value for _, value in assignments]
    sql = f"UPDATE {table} SET {set_sql}"
    if query._where is not None:
        where_sql, where_params = query._where.to_sql(dialect)
        sql += f" WHERE {where_sql}"
        params += where_params
    return sql, params


def compile_delete(query: Query, dialect: Dialect) -> SQL:
    """Make a DELETE statement for the rows of a query."""
    sql = f"DELETE FROM {dialect.quote_name(query.model._meta.table_name)}"
    params: list[Any] = []
    if query._where is not None:
        where_sql, where_params = query._where.to_sql(dialect)
        sql += f" WHERE {where_sql}"
        params += where_params
    return sql, params


def _select_columns(query: Query) -> list[Expression]:
    """Return the expressions of the SELECT list, in order.

    In values mode, the list has the expressions of values(), each with
    an alias. If not, the list has all fields of the query model, then
    all fields of each select_related model. Query._row_to_instance()
    reads the row in the same order.
    """
    if query._values is not None:
        return [_Alias(expression, key) for key, expression in query._values]
    columns: list[Expression] = list(query.model._meta.fields.values())
    for foreign_key in query._select_related:
        columns += list(foreign_key.target_model._meta.fields.values())
    return columns


def _join_expressions(expressions: Any, dialect: Dialect) -> SQL:
    """Make the SQL of each expression and join the texts with commas."""
    texts: list[str] = []
    params: list[Any] = []
    for expression in expressions:
        expression_sql, expression_params = expression.to_sql(dialect)
        texts.append(expression_sql)
        params += expression_params
    return ", ".join(texts), params


class _Alias(Expression):
    """An expression with a name in the SELECT list: ``COUNT(*) AS "total"``."""

    def __init__(self, expression: Expression, alias: str) -> None:
        self.expression = expression
        self.alias = alias

    def to_sql(self, dialect: Dialect) -> SQL:
        expression_sql, params = self.expression.to_sql(dialect)
        return f"{expression_sql} AS {dialect.quote_name(self.alias)}", params
