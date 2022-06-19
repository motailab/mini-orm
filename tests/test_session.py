"""Tests for the Session: unit of work, identity map and transactions."""

import pytest

from mini_orm import IntegrityError, Session, SessionError, StaleDataError, ValidationError

from .conftest import dialect_sql
from .example_models import Author, Book, Category, Country


def test_add_and_commit_inserts_the_row(db, session):
    alice = Author(name="Alice", email="alice@example.com")
    session.add(alice)
    assert db.statements == []  # add() runs no SQL.

    session.commit()

    insert = dialect_sql(db, 'INSERT INTO "authors" ("name", "email") VALUES (?, ?)')
    assert db.statements[0] == "BEGIN"
    assert db.statements[1].startswith(insert)  # PostgreSQL adds RETURNING "id".
    assert db.statements[2:] == ["COMMIT"]
    assert alice.id == 1
    assert alice._state.persisted


def test_flush_inserts_parents_before_children(db, session):
    alice = Author(name="Alice")
    book = Book(title="SQL", author=alice)
    session.add_all([book, alice])  # The child comes first here.
    session.commit()

    inserts = [sql for sql in db.statements if sql.startswith("INSERT")]
    assert inserts[0].startswith(dialect_sql(db, 'INSERT INTO "authors"'))
    assert inserts[1].startswith(dialect_sql(db, 'INSERT INTO "books"'))
    assert book.author_id == alice.id


def test_flush_orders_rows_of_the_same_table(session):
    root = Category(name="root")
    child = Category(name="child", parent=root)
    session.add_all([child, root])
    session.commit()
    assert root.id < child.id
    assert child.parent_id == root.id


def test_a_related_object_must_be_in_the_session(session):
    session.add(Book(title="SQL", author=Author(name="Alice")))
    with pytest.raises(SessionError, match="Add the Author to the session"):
        session.commit()


def test_get_uses_the_identity_map(db, shop):
    with Session(db) as session:
        first = session.get(Book, 1)
        assert len(db.selects()) == 1
        second = session.get(Book, 1)
        assert second is first
        assert len(db.selects()) == 1  # No second SELECT.


def test_query_results_use_the_identity_map(db, shop):
    with Session(db) as session:
        by_get = session.get(Book, 1)
        by_query = session.query(Book).where(Book.title == "SQL Basics").one()
        assert by_query is by_get


def test_get_returns_none_for_a_missing_row_and_validates_the_key(db, shop):
    with Session(db) as session:
        assert session.get(Book, 999) is None
        with pytest.raises(ValidationError):
            session.get(Book, "1")


def test_dirty_checking_updates_only_the_changed_columns(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        book.price = 22.5
        db.clear()
        session.commit()
        update = dialect_sql(db, 'UPDATE "books" SET "price" = ? WHERE "books"."id" = ?')
        assert db.statements == [update, "COMMIT"]


def test_no_change_means_no_update(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        book.price = book.price  # The same value again.
        db.clear()
        session.commit()
        assert db.statements == ["COMMIT"]


def test_change_of_a_related_object_updates_the_foreign_key(db, shop):
    with Session(db) as session:
        book = session.get(Book, 3)
        alice = session.get(Author, 1)
        book.author = alice
        session.commit()
    with Session(db) as session:
        assert session.get(Book, 3).author_id == 1


def test_the_primary_key_cannot_change(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        book.id = 100
        with pytest.raises(SessionError, match="Cannot change the primary key"):
            session.commit()


def test_delete(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        session.delete(book)
        assert session.get(Book, 1) is None  # Marked for deletion.
        session.commit()
        assert not book._state.persisted
    with Session(db) as session:
        assert session.query(Book).count() == 2


def test_delete_of_a_new_instance_only_forgets_it(db, session):
    author = Author(name="Alice")
    session.add(author)
    session.delete(author)
    session.commit()
    assert db.statements == []
    assert author._state.session is None


def test_delete_removes_children_before_parents(db, shop):
    with Session(db) as session:
        alice = session.get(Author, 1)
        books = session.query(Book).where(Book.author == alice).all()
        session.delete(alice)  # The parent comes first here.
        for book in books:
            session.delete(book)
        db.clear()
        session.commit()
    deletes = [sql for sql in db.statements if sql.startswith("DELETE")]
    quote = db.dialect.quote_character
    assert [sql.split(quote)[1] for sql in deletes] == ["books", "books", "authors"]


def test_autoflush_makes_pending_changes_visible_to_queries(session):
    session.add(Author(name="Alice"))
    assert session.query(Author).count() == 1  # Not committed, but flushed.


def test_without_autoflush_a_query_does_not_see_pending_objects(db):
    with Session(db, autoflush=False) as session:
        session.add(Author(name="Alice"))
        assert session.query(Author).count() == 0
        session.flush()
        assert session.query(Author).count() == 1


def test_changes_without_commit_are_lost(db):
    with Session(db) as session:
        session.add(Author(name="Alice"))
        session.flush()
    with Session(db) as session:
        assert session.query(Author).count() == 0


def test_rollback_undoes_the_insert_and_resets_the_instance(db):
    with Session(db) as session:
        alice = Author(name="Alice")
        session.add(alice)
        session.flush()
        assert alice.id == 1
        session.rollback()
        assert alice.id is None
        assert not alice._state.persisted
        assert alice._state.session is None
        # The instance is new again, so the session can insert it again.
        session.add(alice)
        session.commit()
        assert session.query(Author).count() == 1


def test_rollback_restores_the_snapshot_of_updated_and_deleted_instances(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        other = session.get(Book, 2)
        book.price = 99.0
        session.delete(other)
        session.flush()
        session.rollback()
        # The book keeps the new price in memory, but the session knows that
        # the database has the old price: the book is "dirty" again.
        assert book.price == 99.0
        assert book._state.snapshot["price"] == 20.0
        assert other._state.persisted  # The DELETE was undone.


def test_retry_after_an_integrity_error(db):
    with Session(db) as session:
        session.add(Author(name="Alice", email="same@example.com"))
        bob = Author(name="Bob", email="same@example.com")
        session.add(bob)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        bob.email = "bob@example.com"
        session.add(bob)
        session.commit()
        assert session.query(Author).count() == 1


def test_refresh_loads_the_row_again(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        book.price = 99.0  # Not flushed.
        session.refresh(book)
        assert book.price == 20.0


def test_bulk_delete_then_update_raises_stale_data_error(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        session.query(Book).where(Book.id == 1).delete()
        book.price = 50.0
        with pytest.raises(StaleDataError):
            session.commit()


def test_text_primary_key(db):
    with Session(db) as session:
        session.add(Country(code="DE", name="Germany"))
        session.commit()
        assert session.get(Country, "DE").name == "Germany"
        session.add(Country(name="Nowhere"))
        with pytest.raises(SessionError, match="needs a value for its primary key"):
            session.commit()


def test_an_instance_can_be_in_one_session_only(db):
    with Session(db) as first:
        author = Author(name="Alice")
        first.add(author)
        second = Session(db)
        with pytest.raises(SessionError, match="in another session"):
            second.add(author)


def test_a_detached_instance_can_be_added_again(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
    assert book._state.session is None  # close() detached it.
    book.price = 21.0
    with Session(db) as session:
        session.add(book)
        session.commit()
        session.refresh(book)
        assert book.price == 21.0


def test_two_objects_for_one_row_are_not_allowed(db, shop):
    with Session(db) as session:
        old = session.get(Book, 1)
    with Session(db) as session:
        session.get(Book, 1)
        with pytest.raises(SessionError, match="different object"):
            session.add(old)


def test_two_sessions_cannot_use_one_connection_at_the_same_time(db, shop):
    with Session(db) as first, Session(db) as second:
        first.get(Book, 1)  # The first session opens a transaction.
        with pytest.raises(SessionError, match="open transaction"):
            second.get(Book, 2)


def test_add_needs_a_model_instance(session):
    with pytest.raises(TypeError):
        session.add("not a model")


def test_execute_runs_raw_sql_in_the_session_transaction(db, shop):
    with Session(db) as session:
        sql = dialect_sql(db, 'SELECT COUNT(*) FROM "books" WHERE "price" > ?')
        row = session.execute(sql, [21]).fetchone()
        assert row[0] == 2
