"""Tests for relationships: lazy loading, reverse accessors and eager loading."""

import pytest

from mini_orm import IntegrityError, Session, SessionError

from .conftest import dialect_sql
from .example_models import Author, Book, Category


def test_foreign_key_lazy_loading_runs_one_select(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        db.clear()
        author = book.author
        assert author.name == "Alice"
        assert len(db.selects()) == 1
        assert book.author is author  # The second read uses the cache.
        assert len(db.selects()) == 1


def test_foreign_key_lazy_loading_uses_the_identity_map(db, shop):
    with Session(db) as session:
        alice = session.get(Author, 1)
        book = session.get(Book, 1)
        db.clear()
        assert book.author is alice
        assert db.selects() == []


def test_author_id_does_not_load_the_author(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        db.clear()
        assert book.author_id == 1
        assert db.selects() == []


def test_a_new_value_for_the_foreign_key_clears_the_cache(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
        assert book.author.name == "Alice"
        book.author_id = 2
        assert book.author.name == "Bob"


def test_lazy_loading_needs_a_session(db, shop):
    with Session(db) as session:
        book = session.get(Book, 1)
    with pytest.raises(SessionError, match="not in a session"):
        book.author  # noqa: B018


def test_reverse_relation_loads_a_list(db, shop):
    with Session(db) as session:
        alice = session.get(Author, 1)
        titles = [book.title for book in alice.books]
        assert titles == ["SQL Basics", "Advanced SQL"]


def test_reverse_relation_is_cached_and_read_only(db, shop):
    with Session(db) as session:
        alice = session.get(Author, 1)
        books = alice.books
        books.append("something")  # A change to the copy...
        assert len(alice.books) == 2  # ...does not change the cache.
        with pytest.raises(AttributeError, match="read-only"):
            alice.books = []


def test_reverse_relation_of_a_new_instance_is_empty():
    assert Author(name="New").books == []


def test_n_plus_one_queries_without_eager_loading(db, shop):
    with Session(db) as session:
        db.clear()
        books = session.query(Book).order_by("id").all()
        names = [book.author.name for book in books]
        assert names == ["Alice", "Alice", "Bob"]
        # 1 query for the books + 1 query for each different author.
        assert len(db.selects()) == 3


def test_select_related_loads_everything_with_one_select(db, shop):
    with Session(db) as session:
        db.clear()
        books = session.query(Book).select_related("author").order_by("id").all()
        names = [book.author.name for book in books]
        assert names == ["Alice", "Alice", "Bob"]
        assert len(db.selects()) == 1
        assert books[0].author is books[1].author  # The identity map gives one Alice.


def test_select_related_with_a_null_foreign_key(db, shop):
    with Session(db) as session:
        session.add(Book(title="No author"))
        session.commit()
        book = session.query(Book).select_related("author").where(Book.title == "No author").one()
        assert book.author is None


def test_select_related_allows_conditions_on_the_related_model(db, shop):
    with Session(db) as session:
        books = session.query(Book).select_related("author").where(Author.name == "Bob").all()
        assert [book.title for book in books] == ["Python Tricks"]


def test_prefetch_related_for_a_reverse_relation_runs_two_selects(db, shop):
    with Session(db) as session:
        db.clear()
        authors = session.query(Author).prefetch_related("books").all()
        counts = {author.name: len(author.books) for author in authors}
        assert counts == {"Alice": 2, "Bob": 1}
        assert len(db.selects()) == 2
        expected = dialect_sql(db, 'WHERE "books"."author_id" IN (?, ?) ORDER BY "books"."id" ASC')
        assert db.selects()[1].endswith(expected)


def test_prefetch_related_for_a_foreign_key(db, shop):
    with Session(db) as session:
        db.clear()
        books = session.query(Book).prefetch_related("author").all()
        assert {book.author.name for book in books} == {"Alice", "Bob"}
        assert len(db.selects()) == 2


def test_prefetch_related_with_no_results(db):
    with Session(db) as session:
        assert session.query(Author).prefetch_related("books").all() == []


def test_self_referential_relationships(db):
    with Session(db) as session:
        root = Category(name="root")
        session.add_all([root, Category(name="a", parent=root), Category(name="b", parent=root)])
        session.commit()
    with Session(db) as session:
        db.clear()
        roots = session.query(Category).where(Category.parent.is_null()).prefetch_related("children").all()
        assert [child.name for child in roots[0].children] == ["a", "b"]
        assert roots[0].children[0].parent is roots[0]
        assert len(db.selects()) == 2


def test_on_delete_cascade_is_done_by_the_database(db, shop):
    with Session(db) as session:
        session.delete(session.get(Author, 1))
        session.commit()
    with Session(db) as session:
        assert session.query(Book).count() == 1


def test_the_database_checks_foreign_keys(db):
    with Session(db) as session:
        session.add(Book(title="Orphan", author=999))
        with pytest.raises(IntegrityError):
            session.commit()
