"""Relationships between models.

A ForeignKey makes a many-to-one relationship. Many books have one author:

    class Book(Model):
        author = ForeignKey(Author, related_name="books")

The ForeignKey gives these attributes:

* ``book.author``: the Author object. The ORM loads it from the database
  when you read the attribute for the first time (lazy loading).
* ``book.author_id``: the raw value of the "author_id" column. Reading it
  does not load anything.
* ``author.books``: the list of the books of the author (the reverse
  accessor). It exists only if you give ``related_name``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .exceptions import ModelDefinitionError, SessionError, ValidationError
from .fields import Field

if TYPE_CHECKING:
    from .models import Model
    from .session import Session

ON_DELETE_ACTIONS = ("CASCADE", "SET NULL", "RESTRICT", "NO ACTION")


class ForeignKey(Field):
    """A column that holds the primary key of a row in another table.

    Arguments:

    * to: the target model class, or "self" for a model that refers to itself
      (for example a category with a parent category).
    * related_name: the name of the reverse accessor on the target model.
    * on_delete: what the database does with this row when the target row is
      deleted: "CASCADE", "SET NULL", "RESTRICT" or "NO ACTION".

    The instance stores two things:

    * the raw key value in ``instance.__dict__``, like every other field
    * the related object (if it is known) in ``instance._state.related``
    """

    def __init__(
        self,
        to: type[Model] | str,
        *,
        related_name: str | None = None,
        on_delete: str | None = None,
        **options: Any,
    ) -> None:
        refers_to_self = isinstance(to, str) and to == "self"
        if not refers_to_self and not _is_concrete_model(to):
            raise ModelDefinitionError(
                f'ForeignKey needs a model class or "self", not {to!r}. '
                "Define the target model before the model that refers to it."
            )
        if on_delete is not None and on_delete.upper() not in ON_DELETE_ACTIONS:
            raise ModelDefinitionError(
                f"on_delete must be one of {', '.join(ON_DELETE_ACTIONS)}, not {on_delete!r}."
            )
        if on_delete is not None and on_delete.upper() == "SET NULL" and options.get("null") is False:
            raise ModelDefinitionError('on_delete="SET NULL" needs null=True.')
        super().__init__(**options)
        self.to = to
        self.related_name = related_name
        self.on_delete = on_delete.upper() if on_delete is not None else None

    @property
    def target_model(self) -> type[Model]:
        """The model class that this foreign key refers to."""
        if isinstance(self.to, str):
            # "self": the model that has this field. For a field that a child
            # model inherits, that is the child model.
            assert self.model is not None
            return self.model
        return self.to

    # -- Descriptor protocol --------------------------------------------------

    def __set_name__(self, owner: type[Model], name: str) -> None:
        if not self.column:
            self.column = f"{name}_id"
        super().__set_name__(owner, name)
        # Add the "<name>_id" attribute, for example Book.author_id.
        setattr(owner, f"{name}_id", ForeignKeyId(name))

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self
        if self.name in instance._state.related:
            return instance._state.related[self.name]
        key = instance.__dict__.get(self.name)
        if key is None:
            return None
        session = instance._state.session
        if session is None:
            raise SessionError(
                f"Cannot load {self.label}: the {owner.__name__} instance is not in a session."
            )
        # Lazy loading. Session.get() looks in the identity map first, so
        # there is no SQL if the session already has the object.
        related = session.get(self.target_model, key)
        instance._state.related[self.name] = related
        return related

    def __set__(self, instance: Model, value: Any) -> None:
        value = self.validate(value)
        if isinstance(value, self.target_model):
            # Keep the object. If it is new, its primary key is None now.
            # The session copies the key into the column after the INSERT.
            instance._state.related[self.name] = value
            instance.__dict__[self.name] = value.pk
        else:
            # A raw key or None. Forget the old object, it does not match.
            instance._state.related.pop(self.name, None)
            instance.__dict__[self.name] = value

    # -- Validation and conversion --------------------------------------------

    def check_type(self, value: Any) -> Any:
        """Accept an instance of the target model, or a raw primary key value."""
        target = self.target_model
        if isinstance(value, target):
            return value
        try:
            return target._meta.primary_key.check_type(value)
        except ValidationError:
            raise ValidationError(
                f"{self.label} needs an instance of {target.__name__} or a "
                f"primary key value, not {type(value).__name__}."
            ) from None

    def to_db(self, value: Any) -> Any:
        if isinstance(value, self.target_model):
            value = value.pk
        return self.target_model._meta.primary_key.to_db(value)

    def from_db(self, value: Any) -> Any:
        return self.target_model._meta.primary_key.from_db(value)

    # The column has the same type as the primary key that it refers to. The
    # type is known only after Python makes the target model, so these two
    # attributes are properties, not values that __init__ sets.

    @property
    def column_type(self) -> str:  # type: ignore[override]
        return self.target_model._meta.primary_key.column_type

    @property
    def max_length(self) -> int | None:  # type: ignore[override]
        return self.target_model._meta.primary_key.max_length

    # -- Relationship helpers -------------------------------------------------

    def install_reverse_relation(self) -> None:
        """Add the reverse accessor (related_name) to the target model.

        The ModelMeta metaclass calls this method when it makes a model class.
        """
        if self.related_name is None:
            return
        target = self.target_model
        if hasattr(target, self.related_name):
            raise ModelDefinitionError(
                f"{target.__name__} already has an attribute "
                f"{self.related_name!r}. Use a different related_name for {self.label}."
            )
        relation = ReverseRelation(self, self.related_name)
        setattr(target, self.related_name, relation)
        target._meta.reverse_relations[self.related_name] = relation

    def prefetch(self, instances: list[Model], session: Session) -> None:
        """Load the related objects of many instances with one SELECT.

        Query.prefetch_related() calls this method. It runs
        ``SELECT ... WHERE id IN (...)`` and puts each object in the cache
        of the instances that refer to it.
        """
        target = self.target_model
        keys = {instance.__dict__.get(self.name) for instance in instances}
        keys.discard(None)
        related_by_key: dict[Any, Model] = {}
        if keys:
            query = session.query(target).where(target._meta.primary_key.in_(sorted(keys)))
            for related in query.all():
                related_by_key[related.pk] = related
        for instance in instances:
            key = instance.__dict__.get(self.name)
            instance._state.related[self.name] = related_by_key.get(key)


class ForeignKeyId:
    """The "<name>_id" attribute of a ForeignKey, for example ``book.author_id``.

    It reads and writes the raw value of the foreign key column. It never
    loads the related object.
    """

    def __init__(self, foreign_key_name: str) -> None:
        self.foreign_key_name = foreign_key_name

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self.foreign_key_name)

    def __set__(self, instance: Model, value: Any) -> None:
        # The ForeignKey descriptor validates the value and clears the cache.
        setattr(instance, self.foreign_key_name, value)


class ReverseRelation:
    """The "one-to-many" side of a ForeignKey, for example ``author.books``.

    The first read runs a SELECT. The result stays in a cache on the instance.
    Each read gives a new list, so a change to the list does not change the
    cache. To change a relationship, set the ForeignKey on each object, for
    example ``book.author = other_author``.
    """

    def __init__(self, foreign_key: ForeignKey, name: str) -> None:
        self.foreign_key = foreign_key
        self.name = name

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self
        if self.name not in instance._state.related:
            if instance.pk is None:
                # A new instance has no row, so no row can refer to it.
                return []
            session = instance._state.session
            if session is None:
                raise SessionError(
                    f"Cannot load {owner.__name__}.{self.name}: the instance is not in a session."
                )
            # Lazy loading is a prefetch for one instance.
            self.prefetch([instance], session)
        return list(instance._state.related[self.name])

    def __set__(self, instance: Model, value: Any) -> None:
        # A __set__ method makes this a data descriptor, so an assignment
        # cannot hide it behind a value in instance.__dict__.
        raise AttributeError(
            f"{self.name} is read-only. Set {self.foreign_key.label} on each "
            "object to change the relationship."
        )

    def prefetch(self, instances: list[Model], session: Session) -> None:
        """Load the related lists of many instances with one SELECT."""
        foreign_key = self.foreign_key
        child_model = foreign_key.model
        assert child_model is not None
        keys = sorted({instance.pk for instance in instances if instance.pk is not None})
        children_by_key: dict[Any, list[Model]] = {key: [] for key in keys}
        if keys:
            query = (
                session.query(child_model)
                .where(foreign_key.in_(keys))
                .order_by(child_model._meta.primary_key)
            )
            for child in query.all():
                key = child.__dict__[foreign_key.name]
                # With autoflush off, an object in memory can have a changed
                # key that is not in the list. Such an object is not added.
                if key in children_by_key:
                    children_by_key[key].append(child)
        for instance in instances:
            instance._state.related[self.name] = children_by_key.get(instance.pk, [])


def _is_concrete_model(value: Any) -> bool:
    """Return True if the value is a model class that has a table."""
    meta = getattr(value, "_meta", None)
    return isinstance(value, type) and meta is not None and not meta.abstract
