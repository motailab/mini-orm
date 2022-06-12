"""The SQL expression tree.

Python evaluates ``User.age > 18`` when it runs the line. The ``>`` operator
calls ``Field.__gt__``. That method does not compare anything. It returns a
``Comparison`` object, which is one node of an expression tree. Later, the
compiler asks the tree for its SQL text.

Every node has a ``to_sql(dialect)`` method. The method returns a pair:

* the SQL text, with a placeholder for each value (``?`` for SQLite,
  ``%s`` for PostgreSQL and MySQL)
* a list with the values for the placeholders (the parameters)

The ORM never writes a value into the SQL text. The database gets the
values separately, so a value cannot change the meaning of the statement.
This is how the ORM prevents SQL injection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .exceptions import QueryError

if TYPE_CHECKING:
    from .dialects import Dialect

# The return type of every to_sql() method: (SQL text, parameters).
SQL = tuple[str, list[Any]]


class Expression:
    """The base class for all nodes of the expression tree."""

    def to_sql(self, dialect: Dialect) -> SQL:
        """Return the SQL text of this node and its parameters."""
        raise NotImplementedError


class Param(Expression):
    """A value that goes to the database as a parameter."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def to_sql(self, dialect: Dialect) -> SQL:
        return dialect.placeholder, [self.value]


# ---------------------------------------------------------------------------
# Conditions: expressions that are true or false
# ---------------------------------------------------------------------------


class Condition(Expression):
    """An expression that is true or false.

    You use conditions in Query.where(), Query.having() and Query.join().

    Python does not let a class change the ``and``, ``or`` and ``not``
    keywords. So the ORM uses the ``&``, ``|`` and ``~`` operators.
    """

    def __and__(self, other: object) -> Condition:
        if not isinstance(other, Condition):
            # NotImplemented tells Python that this operation is not
            # supported. Python then raises a TypeError.
            return NotImplemented
        return And(self, other)

    def __or__(self, other: object) -> Condition:
        if not isinstance(other, Condition):
            return NotImplemented
        return Or(self, other)

    def __invert__(self) -> Condition:
        return Not(self)

    def __bool__(self) -> bool:
        # This catches a common mistake:
        #     query.where(User.age > 18 and User.age < 65)
        # Python evaluates "a and b" with bool(a). Because a condition object
        # is "true", the result is b only, and the first condition is lost
        # without an error. This method makes the mistake visible.
        raise TypeError(
            "A condition has no True or False value in Python. "
            "Use & for AND, | for OR and ~ for NOT, and put each comparison "
            "in parentheses: (User.age > 18) & (User.age < 65)"
        )


class Comparison(Condition):
    """A comparison of an operand with a value or with another expression.

    Examples of the SQL text: ``"users"."age" > ?`` and
    ``"books"."author_id" = "authors"."id"``.
    """

    def __init__(self, left: Operand, operator: str, right: Any) -> None:
        if right is None:
            raise QueryError(
                f"Cannot use None with the {operator} operator. "
                "Use == None, != None, is_null() or is_not_null()."
            )
        self.left = left
        self.operator = operator
        if isinstance(right, Expression):
            # Another expression, for example a column of a different table.
            self.right = right
        else:
            # A Python value. It becomes a parameter. The left operand
            # validates and converts it, for example 10 becomes 10.0 for a FloatField.
            self.right = Param(left.prepare_value(right))

    def to_sql(self, dialect: Dialect) -> SQL:
        left_sql, left_params = self.left.to_sql(dialect)
        right_sql, right_params = self.right.to_sql(dialect)
        return f"{left_sql} {self.operator} {right_sql}", left_params + right_params


class InList(Condition):
    """A test that the operand is equal to one value in a list: ``x IN (?, ?)``."""

    def __init__(self, operand: Operand, values: Iterable[Any]) -> None:
        # A string is iterable, but in_("abc") is almost always a mistake.
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise QueryError("in_() needs a list, a tuple or a set of values.")
        self.operand = operand
        self.values = [operand.prepare_value(value) for value in values]

    def to_sql(self, dialect: Dialect) -> SQL:
        if not self.values:
            # An empty list matches no row. SQLite accepts "x IN ()", but
            # PostgreSQL and MySQL do not. So the ORM writes a condition that
            # is always false.
            return "1 = 0", []
        operand_sql, params = self.operand.to_sql(dialect)
        placeholders = ", ".join(dialect.placeholder for _ in self.values)
        return f"{operand_sql} IN ({placeholders})", params + self.values


class Between(Condition):
    """A range test: ``x BETWEEN ? AND ?``. Both limits are included."""

    def __init__(self, operand: Operand, low: Any, high: Any) -> None:
        self.operand = operand
        self.low = operand.prepare_value(low)
        self.high = operand.prepare_value(high)

    def to_sql(self, dialect: Dialect) -> SQL:
        operand_sql, params = self.operand.to_sql(dialect)
        placeholder = dialect.placeholder
        return f"{operand_sql} BETWEEN {placeholder} AND {placeholder}", params + [self.low, self.high]


class IsNull(Condition):
    """A NULL test: ``x IS NULL``, or ``x IS NOT NULL`` if negated is True."""

    def __init__(self, operand: Operand, negated: bool = False) -> None:
        self.operand = operand
        self.negated = negated

    def to_sql(self, dialect: Dialect) -> SQL:
        operand_sql, params = self.operand.to_sql(dialect)
        keyword = "IS NOT NULL" if self.negated else "IS NULL"
        return f"{operand_sql} {keyword}", params


class LogicalCondition(Condition):
    """Two conditions joined with a keyword. The subclasses set the keyword."""

    keyword = ""

    def __init__(self, left: Condition, right: Condition) -> None:
        self.left = left
        self.right = right

    def to_sql(self, dialect: Dialect) -> SQL:
        left_sql, left_params = self.left.to_sql(dialect)
        right_sql, right_params = self.right.to_sql(dialect)
        # The parentheses keep the structure of the Python tree. Without
        # them, SQL operator precedence can change the meaning.
        return f"({left_sql} {self.keyword} {right_sql})", left_params + right_params


class And(LogicalCondition):
    """Both conditions must be true."""

    keyword = "AND"


class Or(LogicalCondition):
    """At least one of the conditions must be true."""

    keyword = "OR"


class Not(Condition):
    """The condition must be false."""

    def __init__(self, condition: Condition) -> None:
        self.condition = condition

    def to_sql(self, dialect: Dialect) -> SQL:
        condition_sql, params = self.condition.to_sql(dialect)
        return f"NOT ({condition_sql})", params


# ---------------------------------------------------------------------------
# Operands: expressions that have a value
# ---------------------------------------------------------------------------


class Operand(Expression):
    """An expression with a value: a column (a Field) or an aggregate function.

    The comparison operators of an operand do not return True or False.
    They return a Condition node for the expression tree.
    """

    # When a class defines __eq__, Python sets __hash__ to None for that
    # class, and its objects cannot be dictionary keys. The ORM keeps
    # fields in dictionaries and sets, so it restores the default hash,
    # which uses the identity of the object.
    __hash__ = object.__hash__

    def __eq__(self, other: Any) -> Condition:  # type: ignore[override]
        if other is None:
            # In SQL, "x = NULL" is never true. "x IS NULL" is the correct test.
            return IsNull(self)
        return Comparison(self, "=", other)

    def __ne__(self, other: Any) -> Condition:  # type: ignore[override]
        if other is None:
            return IsNull(self, negated=True)
        return Comparison(self, "<>", other)

    def __lt__(self, other: Any) -> Condition:
        return Comparison(self, "<", other)

    def __le__(self, other: Any) -> Condition:
        return Comparison(self, "<=", other)

    def __gt__(self, other: Any) -> Condition:
        return Comparison(self, ">", other)

    def __ge__(self, other: Any) -> Condition:
        return Comparison(self, ">=", other)

    def like(self, pattern: str) -> Condition:
        """Match a pattern. ``%`` matches any text and ``_`` matches one character."""
        return Comparison(self, "LIKE", pattern)

    def in_(self, values: Iterable[Any]) -> Condition:
        """Match one of the values. The name has a "_" because "in" is a Python keyword."""
        return InList(self, values)

    def between(self, low: Any, high: Any) -> Condition:
        return Between(self, low, high)

    def is_null(self) -> Condition:
        return IsNull(self)

    def is_not_null(self) -> Condition:
        return IsNull(self, negated=True)

    def asc(self) -> Ordering:
        """Sort from the lowest value to the highest value."""
        return Ordering(self, "ASC")

    def desc(self) -> Ordering:
        """Sort from the highest value to the lowest value."""
        return Ordering(self, "DESC")

    def prepare_value(self, value: Any) -> Any:
        """Return the value that the database gets when you compare this operand with it.

        The Field class overrides this method to validate and convert the value.
        """
        return value

    def from_db(self, value: Any) -> Any:
        """Convert a value from the database to a Python value.

        The Field class overrides this method, for example to make a datetime
        from the text that SQLite gives.
        """
        return value


class Star(Operand):
    """The ``*`` argument of ``COUNT(*)``."""

    def to_sql(self, dialect: Dialect) -> SQL:
        return "*", []


class Ordering(Expression):
    """One item of an ORDER BY clause: an operand and a direction."""

    def __init__(self, operand: Operand, direction: str) -> None:
        self.operand = operand
        self.direction = direction

    def to_sql(self, dialect: Dialect) -> SQL:
        operand_sql, params = self.operand.to_sql(dialect)
        return f"{operand_sql} {self.direction}", params


# ---------------------------------------------------------------------------
# Aggregate functions
# ---------------------------------------------------------------------------


class Aggregate(Operand):
    """An SQL aggregate function, for example COUNT or SUM.

    An aggregate is an operand, so you can compare it, for example
    ``Count() > 1``. You use such a condition in Query.having().
    """

    function = ""  # Each subclass sets the name of the SQL function.

    def __init__(self, operand: Operand, *, distinct: bool = False) -> None:
        if not isinstance(operand, Operand):
            raise QueryError(f"{type(self).__name__}() needs a field, not {operand!r}.")
        self.operand = operand
        self.distinct = distinct

    def to_sql(self, dialect: Dialect) -> SQL:
        operand_sql, params = self.operand.to_sql(dialect)
        distinct = "DISTINCT " if self.distinct else ""
        return f"{self.function}({distinct}{operand_sql})", params


class Count(Aggregate):
    """Count rows.

    ``Count()`` makes ``COUNT(*)``, which counts all rows.
    ``Count(Book.price)`` counts only the rows where the price is not NULL.
    """

    function = "COUNT"

    def __init__(self, operand: Operand | None = None, *, distinct: bool = False) -> None:
        super().__init__(Star() if operand is None else operand, distinct=distinct)


class Sum(Aggregate):
    function = "SUM"


class Avg(Aggregate):
    function = "AVG"


class Min(Aggregate):
    function = "MIN"

    def from_db(self, value: Any) -> Any:
        # The smallest value has the type of the column, for example a datetime.
        return self.operand.from_db(value)


class Max(Aggregate):
    function = "MAX"

    def from_db(self, value: Any) -> Any:
        return self.operand.from_db(value)
