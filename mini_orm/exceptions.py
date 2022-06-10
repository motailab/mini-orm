"""Exception classes for mini_orm.

Every error that the ORM raises is a subclass of ORMError. The Database
class also changes the errors of the database drivers into DatabaseError
and IntegrityError, so your code catches the same error for each database.
"""


class ORMError(Exception):
    """The base class for all errors that mini_orm raises."""


class DatabaseError(ORMError):
    """The database refused a statement. The original driver error is in __cause__."""


class IntegrityError(DatabaseError):
    """A constraint failed, for example UNIQUE, NOT NULL or FOREIGN KEY."""


class ModelDefinitionError(ORMError):
    """A model class is not correct. For example, it has two primary keys."""


class ValidationError(ORMError):
    """A value is not correct for a field. For example, the type is wrong."""


class QueryError(ORMError):
    """A query is not correct. For example, it uses an unknown field."""


class DoesNotExist(ORMError):
    """Query.one() found no row."""


class MultipleObjectsReturned(ORMError):
    """Query.one() found more than one row."""


class SessionError(ORMError):
    """The session cannot do the operation in its current state."""


class StaleDataError(SessionError):
    """An UPDATE or a DELETE statement changed no row, so the row is gone."""


class CircularDependencyError(ORMError):
    """Items depend on each other in a circle, so no order is possible."""


class MigrationError(ORMError):
    """A migration cannot be made, loaded, applied or unapplied."""


class IrreversibleError(MigrationError):
    """A migration operation has no backwards SQL, so it cannot be unapplied."""
