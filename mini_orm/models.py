"""Model classes: the Python side of the database tables.

You define a table as a class:

    class User(Model):
        __tablename__ = "users"
        name = StringField(null=False)
        age = IntegerField()

When Python makes the class, it calls the ModelMeta metaclass. The
metaclass collects the fields and puts the table information in
``User._meta``.
"""

from __future__ import annotations

import copy
from typing import Any

from .exceptions import ModelDefinitionError, QueryError
from .fields import Field, IntegerField
from .relations import ForeignKey, ReverseRelation


class InstanceState:
    """The ORM data for one model instance.

    The ORM keeps its own data apart from the field values. The field
    values are in ``instance.__dict__``. This object is ``instance._state``.
    """

    def __init__(self) -> None:
        # The session that tracks the instance, or None.
        self.session: Any = None
        # The column values after the last load or flush, as the fields give
        # them to the database (to_db). The session compares them with the current values to find
        # the changes (dirty checking). None means that the instance has
        # no row in the database.
        self.snapshot: dict[str, Any] | None = None
        # A cache for relationship attributes: the related object of each
        # ForeignKey and the list of each ReverseRelation.
        self.related: dict[str, Any] = {}

    @property
    def persisted(self) -> bool:
        """True if the instance has a row in the database."""
        return self.snapshot is not None


class ModelMetadata:
    """Information about one model class. The ORM keeps it in ``Model._meta``."""

    def __init__(
        self,
        model: type,
        table_name: str,
        fields: dict[str, Field],
        abstract: bool,
    ) -> None:
        self.model = model
        self.table_name = table_name
        # Attribute name -> Field, in column order.
        self.fields = fields
        self.abstract = abstract
        # Name -> ReverseRelation. ForeignKey.install_reverse_relation() adds them.
        self.reverse_relations: dict[str, ReverseRelation] = {}
        primary_keys = [field for field in fields.values() if field.primary_key]
        # An abstract model can have no primary key.
        self.primary_key: Field = primary_keys[0] if primary_keys else None  # type: ignore[assignment]

    @property
    def foreign_keys(self) -> list[ForeignKey]:
        return [field for field in self.fields.values() if isinstance(field, ForeignKey)]

    def get_field(self, name: str) -> Field:
        """Return the field with the attribute name. Raise QueryError if it does not exist."""
        if name not in self.fields:
            raise QueryError(f"{self.model.__name__} has no field {name!r}.")
        return self.fields[name]

    def get_relation(self, name: str) -> ForeignKey | ReverseRelation:
        """Return the ForeignKey or the ReverseRelation with the name."""
        field = self.fields.get(name)
        if isinstance(field, ForeignKey):
            return field
        if name in self.reverse_relations:
            return self.reverse_relations[name]
        raise QueryError(f"{self.model.__name__} has no relationship {name!r}.")


class ModelMeta(type):
    """The metaclass of Model.

    Python calls ``ModelMeta.__new__`` when it makes a model class, so the
    method runs once for each model, at import time. It does these steps:

    1. It copies the fields of the parent models.
    2. It adds an ``id`` primary key if the model has no primary key.
    3. It makes the class. Python then calls ``__set_name__`` on each field.
    4. It puts the table information in ``cls._meta``.
    5. It adds the reverse accessor of each ForeignKey to the target model.
    """

    # Each class that this metaclass makes (except Model itself) has _meta.
    _meta: ModelMetadata

    def __new__(mcls, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> ModelMeta:
        parents = [base for base in bases if hasattr(base, "_meta")]
        if not any(isinstance(base, ModelMeta) for base in bases):
            # This is the Model base class. It has no table and no fields.
            return super().__new__(mcls, name, bases, namespace)

        abstract = bool(namespace.get("__abstract__", False))
        # The fields of the parents come first, so the columns of a parent
        # come before the columns of the child. A field in the class body
        # replaces a parent field with the same name.
        namespace = {**_copy_parent_fields(parents), **namespace}
        has_primary_key = any(isinstance(value, Field) and value.primary_key for value in namespace.values())
        if not abstract and not has_primary_key:
            namespace = {"id": IntegerField(primary_key=True), **namespace}

        cls = super().__new__(mcls, name, bases, namespace)

        fields = {key: value for key, value in namespace.items() if isinstance(value, Field)}
        _check_primary_keys(name, fields)
        table_name = namespace.get("__tablename__") or name.lower()
        cls._meta = ModelMetadata(cls, table_name, fields, abstract)

        if not abstract:
            for foreign_key in cls._meta.foreign_keys:
                foreign_key.install_reverse_relation()
        return cls


def _copy_parent_fields(parents: list[type]) -> dict[str, Field]:
    """Return copies of the fields of the parent models.

    Each model needs its own field objects. A field knows its model
    (``field.model``), and the model gives the table name for the SQL.
    Python calls ``__set_name__`` on each copy, which sets the new model.
    """
    copies: dict[str, Field] = {}
    # The first parent wins if two parents have a field with the same name,
    # as in the Python method resolution order.
    for parent in reversed(parents):
        for field_name, field in parent._meta.fields.items():  # type: ignore[attr-defined]
            copies[field_name] = copy.copy(field)
    return copies


def _check_primary_keys(class_name: str, fields: dict[str, Field]) -> None:
    primary_keys = [field_name for field_name, field in fields.items() if field.primary_key]
    if len(primary_keys) > 1:
        raise ModelDefinitionError(
            f"{class_name} has more than one primary key: {', '.join(primary_keys)}. "
            "mini_orm supports one primary key column for each table."
        )


class Model(metaclass=ModelMeta):
    """The base class of all models.

    Each subclass is one table. Each instance is one row.
    """

    _meta: ModelMetadata  # The metaclass sets it on each model class.
    _state: InstanceState  # __init__ and _from_db set it on each instance.

    def __init__(self, **values: Any) -> None:
        if self._meta.abstract:
            raise TypeError(f"{type(self).__name__} is abstract. Make an instance of a subclass.")
        self._state = InstanceState()
        unknown = sorted(set(values) - set(self._meta.fields))
        if unknown:
            raise TypeError(f"{type(self).__name__} has no field: {', '.join(unknown)}")
        # setattr() calls Field.__set__, which validates each value.
        for field_name, field in self._meta.fields.items():
            if field_name in values:
                setattr(self, field_name, values[field_name])
            else:
                setattr(self, field_name, field.get_default())

    @classmethod
    def _from_db(cls, db_values: dict[str, Any]) -> Model:
        """Make an instance from the values of a database row.

        This method does not call __init__. The values come from the
        database, so the ORM does not validate them again. It only converts
        them to Python types. A subclass can also change __init__ without
        an effect on the loading of rows.
        """
        instance = cls.__new__(cls)
        instance._state = InstanceState()
        instance._set_db_values(db_values)
        return instance

    def _set_db_values(self, db_values: dict[str, Any]) -> None:
        """Put the values of a database row into the instance, without validation."""
        for field_name, field in self._meta.fields.items():
            self.__dict__[field_name] = field.from_db(db_values[field_name])

    @property
    def pk(self) -> Any:
        """The value of the primary key."""
        return self.__dict__.get(self._meta.primary_key.name)

    def __repr__(self) -> str:
        # Read __dict__, not the attributes: a ForeignKey attribute can run
        # a query, and repr() must never run a query.
        values = ", ".join(
            f"{field_name}={self.__dict__.get(field_name)!r}" for field_name in self._meta.fields
        )
        return f"{type(self).__name__}({values})"
