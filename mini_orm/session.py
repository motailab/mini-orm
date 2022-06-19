"""The Session: the Unit of Work and the Identity Map.

You do all work with objects in a session:

    with Session(db) as session:
        alice = Author(name="Alice")
        session.add(alice)              # no SQL yet
        session.commit()                # INSERT, then COMMIT

        book = session.get(Book, 1)     # SELECT, or no SQL (identity map)
        book.price = 30.0               # no SQL yet
        session.commit()                # UPDATE "books" SET "price" = ? ...

The session uses two patterns:

* Identity Map: the session keeps one object for each row. Two loads of
  the same row give the same Python object (``a is b`` is True).
* Unit of Work: the session records the new, changed and deleted objects.
  flush() sends all changes to the database in the correct order.
  commit() makes them permanent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from .compiler import compile_delete, compile_insert, compile_update
from .exceptions import SessionError, StaleDataError
from .fields import IntegerField
from .graph import sort_by_dependencies
from .models import Model
from .query import Query

if TYPE_CHECKING:
    from .database import Database

IdentityKey = tuple[type[Model], Any]


class Session:
    """A unit of work with one database.

    * database: the Database to use.
    * autoflush: if True, the session flushes the pending changes before it
      runs a query. Then the query results include the changes.
    """

    def __init__(self, database: Database, *, autoflush: bool = True) -> None:
        self.database = database
        self.autoflush = autoflush
        # (model class, primary key value) -> instance. One object for each row.
        self._identity_map: dict[IdentityKey, Model] = {}
        # Instances to INSERT at the next flush, in the order of add().
        self._new: list[Model] = []
        # Instances to DELETE at the next flush.
        self._deleted: list[Model] = []
        # For rollback(): the state of each instance before the session
        # changed it in the current transaction. Each item is
        # (instance, snapshot before, primary key before).
        self._undo_log: list[tuple[Model, dict[str, Any] | None, Any]] = []
        self._in_transaction = False

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Changes that are not committed are lost. Call commit() to keep them.
        self.close()

    # -- Reading --------------------------------------------------------------

    def query(self, model: type[Model]) -> Query:
        """Start a query for the model."""
        return Query(model, session=self)

    def get(self, model: type[Model], pk: Any) -> Model | None:
        """Return the instance with the primary key, or None if there is no row.

        If the identity map has the instance, this method runs no SQL.
        """
        primary_key = model._meta.primary_key
        pk = primary_key.validate(pk)
        instance = self._identity_map.get((model, pk))
        if instance is not None:
            return None if instance in self._deleted else instance
        return self.query(model).where(primary_key == pk).first()

    # -- Changing -------------------------------------------------------------

    def add(self, instance: Model) -> None:
        """Put an instance in the session.

        A new instance becomes pending: the next flush inserts it. An
        instance that has a row (for example from a closed session) is
        tracked again: the next flush updates its changed columns.
        """
        if not isinstance(instance, Model):
            raise TypeError(f"add() needs a model instance, not {type(instance).__name__}.")
        state = instance._state
        if state.session is self:
            if instance in self._deleted:
                # add() after delete() cancels the delete.
                self._deleted.remove(instance)
            return
        if state.session is not None:
            raise SessionError(f"{instance!r} is in another session.")
        if state.persisted:
            key = _identity_key(instance)
            if key in self._identity_map:
                raise SessionError(f"The session already has a different object for the row of {instance!r}.")
            self._identity_map[key] = instance
        else:
            self._new.append(instance)
        state.session = self

    def add_all(self, instances: Iterable[Model]) -> None:
        for instance in instances:
            self.add(instance)

    def delete(self, instance: Model) -> None:
        """Mark an instance for deletion. The next flush deletes its row."""
        if instance._state.session is not self:
            raise SessionError(f"{instance!r} is not in this session.")
        if instance in self._new:
            # The instance has no row yet, so the session only forgets it.
            self._new.remove(instance)
            instance._state.session = None
        elif instance not in self._deleted:
            self._deleted.append(instance)

    # -- Unit of work ---------------------------------------------------------

    def flush(self) -> None:
        """Send all pending changes to the database, in the current transaction.

        The order is: all INSERTs, then all UPDATEs, then all DELETEs.
        The INSERTs come in dependency order (a parent row before its
        children). The DELETEs come in the reverse order.
        """
        self._flush_inserts()
        self._flush_updates()
        self._flush_deletes()

    def commit(self) -> None:
        """Flush the changes, then make them permanent (COMMIT)."""
        self.flush()
        if self._in_transaction:
            self.database.commit()
            self._in_transaction = False
        self._undo_log = []

    def rollback(self) -> None:
        """Undo all changes since the last commit (ROLLBACK).

        After a rollback, the session is empty and all its instances are
        detached. Each instance gets back the snapshot and the primary key
        that it had before the transaction. So an instance knows again if
        it has a row. The values that you changed stay in the instance.
        """
        if self._in_transaction:
            self.database.rollback()
            self._in_transaction = False
        for instance, snapshot, pk in reversed(self._undo_log):
            instance._state.snapshot = snapshot
            instance.__dict__[instance._meta.primary_key.name] = pk
        self._undo_log = []
        self._detach_all()

    def close(self) -> None:
        """Roll back the changes that are not committed and detach all instances.

        You can use the session again after close().
        """
        self.rollback()

    def refresh(self, instance: Model) -> None:
        """Load the column values of the instance again from the database.

        Changes that are not flushed are lost. The cached relationships are
        cleared, so the next read of a relationship loads it again.
        """
        if instance._state.session is not self or not instance._state.persisted:
            raise SessionError(f"{instance!r} is not a persisted instance of this session.")
        model = type(instance)
        query = Query(model).where(model._meta.primary_key == instance.pk)
        sql, params = query.to_sql(self.database.dialect)
        row = self._execute(sql, params).fetchone()
        if row is None:
            raise StaleDataError(f"The row of {instance!r} does not exist.")
        self._remember(instance)
        instance._set_db_values(dict(zip(model._meta.fields, row, strict=True)))
        instance._state.snapshot = take_snapshot(instance)
        instance._state.related.clear()

    # -- SQL ------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one SQL statement in the transaction of the session.

        If autoflush is on, flush the pending changes first.
        """
        if self.autoflush:
            self.flush()
        return self._execute(sql, params)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one SQL statement. Start a transaction if none is open. Do not flush."""
        if not self._in_transaction:
            if self.database.in_transaction:
                raise SessionError(
                    "The database connection has an open transaction from a "
                    "different session. Use one session at a time."
                )
            self.database.begin()
            self._in_transaction = True
        return self.database.execute(sql, params)

    # -- Loading --------------------------------------------------------------

    def _load_row(self, model: type[Model], values: Sequence[Any]) -> Model | None:
        """Return the instance for one row. Use the identity map.

        ``values`` has one value for each field of the model, in field order.
        If the session already has an instance for the row, the method gives
        that instance and does not change it. So a change that is not
        flushed stays in the instance.
        """
        db_values = dict(zip(model._meta.fields, values, strict=True))
        primary_key = model._meta.primary_key
        raw_pk = db_values[primary_key.name]
        if raw_pk is None:
            # A LEFT JOIN found no related row.
            return None
        key = (model, primary_key.from_db(raw_pk))
        instance = self._identity_map.get(key)
        if instance is None:
            instance = model._from_db(db_values)
            instance._state.session = self
            instance._state.snapshot = take_snapshot(instance)
            self._identity_map[key] = instance
        return instance

    # -- Flush steps ----------------------------------------------------------

    def _flush_inserts(self) -> None:
        ordered = sort_by_dependencies(self._new, _related_objects)
        try:
            for instance in ordered:
                self._insert(instance)
        finally:
            # After an error, keep the instances that did not get a row.
            self._new = [instance for instance in self._new if not instance._state.persisted]

    def _insert(self, instance: Model) -> None:
        _copy_foreign_keys(instance)
        primary_key = instance._meta.primary_key
        if instance.pk is None and not isinstance(primary_key, IntegerField):
            raise SessionError(
                f"{type(instance).__name__} needs a value for its primary key "
                f"{primary_key.name!r}. Only an IntegerField primary key gets a value from the database."
            )
        self._remember(instance)
        sql, params = compile_insert(instance, self.database.dialect)
        cursor = self._execute(sql, params)
        if instance.pk is None:
            # The database gave the primary key a value. Copy it to the instance.
            instance.__dict__[primary_key.name] = self.database.dialect.new_primary_key(cursor)
        instance._state.snapshot = take_snapshot(instance)
        self._identity_map[_identity_key(instance)] = instance

    def _flush_updates(self) -> None:
        deleted = set(self._deleted)
        for instance in list(self._identity_map.values()):
            if instance in deleted:
                continue
            _copy_foreign_keys(instance)
            changes = changed_values(instance)
            if changes:
                self._update(instance, changes)

    def _update(self, instance: Model, changes: dict[str, Any]) -> None:
        meta = instance._meta
        if meta.primary_key.name in changes:
            raise SessionError(f"Cannot change the primary key of {instance!r}, because it has a row.")
        self._remember(instance)
        query = Query(type(instance)).where(meta.primary_key == instance.pk)
        assignments = [(meta.fields[name], value) for name, value in changes.items()]
        sql, params = compile_update(query, assignments, self.database.dialect)
        if self._execute(sql, params).rowcount == 0:
            raise StaleDataError(f"Cannot update {instance!r}: its row does not exist.")
        instance._state.snapshot = take_snapshot(instance)

    def _flush_deletes(self) -> None:
        deleted_by_key = {_identity_key(instance): instance for instance in self._deleted}
        ordered = sort_by_dependencies(self._deleted, lambda instance: _parents_in(instance, deleted_by_key))
        try:
            # A child row must go before its parent row, so use the reverse order.
            for instance in reversed(ordered):
                self._delete(instance)
        finally:
            self._deleted = [instance for instance in self._deleted if instance._state.persisted]

    def _delete(self, instance: Model) -> None:
        self._remember(instance)
        query = Query(type(instance)).where(instance._meta.primary_key == instance.pk)
        sql, params = compile_delete(query, self.database.dialect)
        if self._execute(sql, params).rowcount == 0:
            raise StaleDataError(f"Cannot delete {instance!r}: its row does not exist.")
        del self._identity_map[_identity_key(instance)]
        instance._state.snapshot = None
        instance._state.session = None

    # -- Helpers --------------------------------------------------------------

    def _remember(self, instance: Model) -> None:
        """Record the state of an instance before a change, for rollback()."""
        self._undo_log.append((instance, instance._state.snapshot, instance.pk))

    def _detach_all(self) -> None:
        for instance in [*self._identity_map.values(), *self._new, *self._deleted]:
            instance._state.session = None
        self._identity_map = {}
        self._new = []
        self._deleted = []


def take_snapshot(instance: Model) -> dict[str, Any]:
    """Return the database value of each field of the instance."""
    return {name: field.to_db(instance.__dict__.get(name)) for name, field in instance._meta.fields.items()}


def changed_values(instance: Model) -> dict[str, Any]:
    """Return the fields whose database value is not the same as in the snapshot.

    This is "dirty checking". The snapshot holds database values, for
    example the ISO text of a datetime. So the comparison is simple and a
    mutable value cannot change the snapshot.
    """
    snapshot = instance._state.snapshot or {}
    return {name: value for name, value in take_snapshot(instance).items() if value != snapshot.get(name)}


def _identity_key(instance: Model) -> IdentityKey:
    return (type(instance), instance.pk)


def _copy_foreign_keys(instance: Model) -> None:
    """Copy the primary key of each related object into the foreign key column.

    A related object can be new when you assign it (book.author = Author()).
    It gets its primary key only at its INSERT. The flush inserts it first,
    then this function copies the new key into the instance.
    """
    for foreign_key in instance._meta.foreign_keys:
        related = instance._state.related.get(foreign_key.name)
        if related is None:
            continue
        if related.pk is None:
            raise SessionError(
                f"{foreign_key.label} refers to a {type(related).__name__} that has no "
                f"primary key. Add the {type(related).__name__} to the session."
            )
        instance.__dict__[foreign_key.name] = related.pk


def _related_objects(instance: Model) -> list[Model]:
    """Return the objects that the foreign keys of the instance refer to.

    The flush uses them to sort the INSERTs.
    """
    related = [instance._state.related.get(foreign_key.name) for foreign_key in instance._meta.foreign_keys]
    return [obj for obj in related if obj is not None]


def _parents_in(instance: Model, instances_by_key: dict[IdentityKey, Model]) -> list[Model]:
    """Return the instances in ``instances_by_key`` that the instance refers to.

    The flush uses them to sort the DELETEs. The function uses the raw key
    values, because a deleted instance often has no cached related object.
    """
    parents = []
    for foreign_key in instance._meta.foreign_keys:
        key = (foreign_key.target_model, instance.__dict__.get(foreign_key.name))
        if key in instances_by_key:
            parents.append(instances_by_key[key])
    return parents
