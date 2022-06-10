"""mini_orm: a small ORM for SQLite, PostgreSQL and MySQL, written to learn how an ORM works.

The public API:

    from mini_orm import Database, Session, Model, StringField, ForeignKey

    class Author(Model):
        __tablename__ = "authors"
        name = StringField(null=False)

    db = Database(":memory:", echo=True)
    db.create_tables([Author])
    with Session(db) as session:
        session.add(Author(name="Alice"))
        session.commit()
        authors = session.query(Author).where(Author.name.like("A%")).all()
"""

from .database import Database
from .dialects import Dialect, MySQLDialect, PostgreSQLDialect, SQLiteDialect
from .exceptions import (
    CircularDependencyError,
    DatabaseError,
    DoesNotExist,
    IntegrityError,
    IrreversibleError,
    MigrationError,
    ModelDefinitionError,
    MultipleObjectsReturned,
    ORMError,
    QueryError,
    SessionError,
    StaleDataError,
    ValidationError,
)
from .expressions import Avg, Count, Max, Min, Sum
from .fields import (
    BooleanField,
    DateField,
    DateTimeField,
    Field,
    FloatField,
    IntegerField,
    StringField,
)
from .models import Model
from .query import Query
from .relations import ForeignKey
from .schema import create_table_sql, drop_table_sql
from .session import Session

__all__ = [
    "Avg",
    "BooleanField",
    "CircularDependencyError",
    "Count",
    "Database",
    "DatabaseError",
    "DateField",
    "DateTimeField",
    "Dialect",
    "DoesNotExist",
    "Field",
    "FloatField",
    "ForeignKey",
    "IntegerField",
    "IntegrityError",
    "IrreversibleError",
    "Max",
    "MigrationError",
    "Min",
    "Model",
    "ModelDefinitionError",
    "MultipleObjectsReturned",
    "MySQLDialect",
    "ORMError",
    "PostgreSQLDialect",
    "Query",
    "QueryError",
    "SQLiteDialect",
    "Session",
    "SessionError",
    "StaleDataError",
    "StringField",
    "Sum",
    "ValidationError",
    "create_table_sql",
    "drop_table_sql",
]
