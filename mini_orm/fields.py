"""Field classes. A field describes one column of a table.

A field object has three jobs:

1. It is a descriptor. It stores and validates the value of one attribute
   on each model instance, for example ``user.age = 30``.
2. It is an operand of the expression tree. On the class, ``User.age > 18``
   makes a Comparison node for a WHERE clause.
3. It converts values between Python and the database (``to_db`` and ``from_db``).

A field does not know the database. It gives a standard Python value: an
int, a float, a str, a bool, a date or a datetime. The dialect of the
database adapts that value for its driver (see dialects.py).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from .exceptions import ValidationError
from .expressions import SQL, Operand

if TYPE_CHECKING:
    from .dialects import Dialect
    from .models import Model


class Field(Operand):
    """The base class for all fields.

    Options:

    * primary_key: the field is the primary key of the table.
    * null: the value can be None (NULL in the database).
    * default: the value for a new instance if you give no value. It can be
      a function, for example ``datetime.datetime.now``. Then the ORM calls
      the function for each new instance.
    * unique: no two rows can have the same value (a UNIQUE constraint).
    * column: the column name in the table. The default is the attribute name.
    """

    python_type: type = object  # The Python type that the field accepts.
    # The column type, for example "integer". It does not depend on the
    # database. The dialect gives the SQL type, for example "INTEGER".
    column_type = ""
    max_length: int | None = None

    def __init__(
        self,
        *,
        primary_key: bool = False,
        null: bool = True,
        default: Any = None,
        unique: bool = False,
        column: str | None = None,
    ) -> None:
        self.primary_key = primary_key
        self.null = null
        self.default = default
        self.unique = unique
        # An empty column name means: use the attribute name (see __set_name__).
        self.column = column or ""
        # __set_name__ sets these two attributes when Python makes the class.
        self.name = ""
        self.model: type[Model] | None = None

    # -- Descriptor protocol --------------------------------------------------

    def __set_name__(self, owner: type[Model], name: str) -> None:
        """Python calls this method once, when it makes the model class."""
        self.name = name
        self.model = owner
        if not self.column:
            self.column = name

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            # Access on the class (User.age) gives the field itself.
            # The field is an operand, so User.age > 18 makes a condition.
            return self
        # Access on an instance (user.age) gives the value.
        return instance.__dict__.get(self.name)

    def __set__(self, instance: Model, value: Any) -> None:
        instance.__dict__[self.name] = self.validate(value)

    # -- Validation -------------------------------------------------------------

    def validate(self, value: Any) -> Any:
        """Return the value if it is correct for the field. If not, raise ValidationError.

        The method can return a changed value, for example a FloatField
        changes the int 10 to the float 10.0.
        """
        if value is None:
            # A primary key is None until the database gives it a value.
            if self.null or self.primary_key:
                return None
            raise ValidationError(f"{self.label} cannot be None.")
        return self.check_type(value)

    def check_type(self, value: Any) -> Any:
        """Validate a value that is not None. Subclasses add more checks."""
        if not isinstance(value, self.python_type):
            raise ValidationError(
                f"{self.label} must be {self.python_type.__name__}, not {type(value).__name__}."
            )
        return value

    def get_default(self) -> Any:
        """Return the default value for a new instance."""
        if callable(self.default):
            return self.default()
        return self.default

    # -- Conversion between Python and the database ---------------------------

    def to_db(self, value: Any) -> Any:
        """Convert a Python value to a standard value for the database.

        The built-in fields need no conversion. A field for another Python
        type overrides this method, for example to change a dict into JSON text.
        """
        return value

    def from_db(self, value: Any) -> Any:
        """Convert a value from the database to a Python value."""
        return value

    def prepare_value(self, value: Any) -> Any:
        """Validate and convert a value that a query compares with this field."""
        return self.to_db(self.validate(value))

    # -- SQL ------------------------------------------------------------------

    def to_sql(self, dialect: Dialect) -> SQL:
        """Return the full column name, for example ``"users"."age"``."""
        assert self.model is not None, "The field is not part of a model."
        table = dialect.quote_name(self.model._meta.table_name)
        return f"{table}.{dialect.quote_name(self.column)}", []

    # -- Helpers --------------------------------------------------------------

    @property
    def label(self) -> str:
        """The name for error messages, for example "User.age"."""
        model_name = self.model.__name__ if self.model is not None else "?"
        return f"{model_name}.{self.name}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label}>"


class IntegerField(Field):
    """A whole number.

    An IntegerField primary key gets its value from the database: an
    INTEGER PRIMARY KEY in SQLite, an identity column in PostgreSQL and an
    AUTO_INCREMENT column in MySQL.
    """

    python_type = int
    column_type = "integer"

    def check_type(self, value: Any) -> Any:
        # bool is a subclass of int, so isinstance(True, int) is True.
        # A bool in an integer column is almost always a mistake.
        if isinstance(value, bool):
            raise ValidationError(f"{self.label} must be int, not bool.")
        return super().check_type(value)


class FloatField(Field):
    """A number with a decimal part (a floating-point number)."""

    python_type = float
    column_type = "float"

    def check_type(self, value: Any) -> Any:
        # Accept an int, for example 10, and keep it as the float 10.0.
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        return super().check_type(value)


class StringField(Field):
    """Text. With max_length, the column is VARCHAR(n) and the ORM refuses longer text.

    SQLite does not enforce the length of VARCHAR(n). So the ORM does the
    check in Python, the same for each database.
    """

    python_type = str
    column_type = "string"

    def __init__(self, *, max_length: int | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.max_length = max_length

    def check_type(self, value: Any) -> Any:
        value = super().check_type(value)
        if self.max_length is not None and len(value) > self.max_length:
            raise ValidationError(
                f"{self.label} can have {self.max_length} characters at most, not {len(value)}."
            )
        return value


class BooleanField(Field):
    """True or False."""

    python_type = bool
    column_type = "boolean"

    def from_db(self, value: Any) -> Any:
        # SQLite and MySQL give 1 or 0. PostgreSQL gives True or False.
        return None if value is None else bool(value)


class DateTimeField(Field):
    """A date and a time, without a time zone.

    SQLite has no date type, so it stores the ISO 8601 text, for example
    "2022-05-01T14:30:00". The text sorts in time order, so comparisons and
    ORDER BY work. PostgreSQL and MySQL store a real date and time.
    """

    python_type = datetime.datetime
    column_type = "datetime"

    def from_db(self, value: Any) -> Any:
        # SQLite gives the text. PostgreSQL and MySQL give a datetime.
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        return value


class DateField(Field):
    """A date without a time. SQLite stores the ISO 8601 text, for example "2022-05-01"."""

    python_type = datetime.date
    column_type = "date"

    def check_type(self, value: Any) -> Any:
        # datetime is a subclass of date. A datetime here would lose its time.
        if isinstance(value, datetime.datetime):
            raise ValidationError(f"{self.label} must be date, not datetime.")
        return super().check_type(value)

    def from_db(self, value: Any) -> Any:
        # SQLite gives the text. PostgreSQL and MySQL give a date.
        if isinstance(value, str):
            return datetime.date.fromisoformat(value)
        return value
