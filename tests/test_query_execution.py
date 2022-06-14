"""Tests that run queries on a database with data."""

import datetime

import pytest

from mini_orm import (
    Avg,
    Count,
    DoesNotExist,
    Max,
    Min,
    MultipleObjectsReturned,
    QueryError,
    Session,
    Sum,
    ValidationError,
)

from .conftest import dialect_sql
from .example_models import Author, Book


@pytest.fixture
def session(db, shop):
    with Session(db) as session:
        yield session


def titles(books):
    return [book.title for book in books]


def test_all_and_iteration(session):
    query = session.query(Book).order_by("id")
    assert titles(query.all()) == ["SQL Basics", "Advanced SQL", "Python Tricks"]
    assert titles(query) == ["SQL Basics", "Advanced SQL", "Python Tricks"]


def test_where_with_different_operators(session):
    assert titles(session.query(Book).where(Book.price >= 25).order_by("price")) == [
        "Python Tricks",
        "Advanced SQL",
    ]
    assert titles(session.query(Book).where(Book.title.like("%SQL%")).order_by("id")) == [
        "SQL Basics",
        "Advanced SQL",
    ]
    assert titles(session.query(Book).where(Book.id.in_([1, 3])).order_by("id")) == [
        "SQL Basics",
        "Python Tricks",
    ]
    assert titles(session.query(Book).where(~(Book.published == True))) == ["Python Tricks"]  # noqa: E712
    assert titles(session.query(Book).where((Book.price < 21) | (Book.price > 30)).order_by("price")) == [
        "SQL Basics",
        "Advanced SQL",
    ]


def test_order_limit_and_offset(session):
    query = session.query(Book).order_by(Book.price.desc())
    assert titles(query.limit(2)) == ["Advanced SQL", "Python Tricks"]
    assert titles(query.limit(2).offset(1)) == ["Python Tricks", "SQL Basics"]
    assert titles(query.offset(2)) == ["SQL Basics"]


def test_first_and_one(session):
    assert session.query(Book).order_by("-price").first().title == "Advanced SQL"
    assert session.query(Book).where(Book.price > 100).first() is None
    assert session.query(Book).where(Book.price == 20).one().title == "SQL Basics"
    with pytest.raises(DoesNotExist):
        session.query(Book).where(Book.price > 100).one()
    with pytest.raises(MultipleObjectsReturned):
        session.query(Book).one()


def test_count_and_exists(session):
    assert session.query(Book).count() == 3
    assert session.query(Book).where(Book.published == True).count() == 2  # noqa: E712
    assert session.query(Book).limit(2).count() == 2
    assert session.query(Book).group_by(Book.author).values(Book.author).count() == 2
    assert session.query(Book).where(Book.price > 30).exists()
    assert not session.query(Book).where(Book.price > 100).exists()


def test_aggregate(session):
    result = session.query(Book).aggregate(
        total=Count(),
        price_sum=Sum(Book.price),
        average=Avg(Book.price),
        cheapest=Min(Book.price),
        most_expensive=Max(Book.price),
    )
    assert result == {
        "total": 3,
        "price_sum": 80.0,
        "average": pytest.approx(26.666, rel=1e-3),
        "cheapest": 20.0,
        "most_expensive": 35.0,
    }
    with pytest.raises(QueryError):
        session.query(Book).limit(1).aggregate(total=Count())


def test_aggregate_with_a_filter_on_a_related_model(session):
    result = session.query(Book).join(Author).where(Author.name == "Alice").aggregate(total=Sum(Book.price))
    assert result == {"total": 55.0}


def test_values_returns_dicts(session):
    rows = session.query(Book).where(Book.id == 1).values(Book.title, Book.published, Book.author).all()
    assert rows == [{"title": "SQL Basics", "published": True, "author": 1}]


def test_group_by_with_having(session):
    rows = (
        session.query(Book)
        .group_by(Book.author)
        .having(Count() > 1)
        .values(Book.author, books=Count(), average=Avg(Book.price))
        .all()
    )
    assert rows == [{"author": 1, "books": 2, "average": 27.5}]


def test_join_one_to_many_with_distinct(session):
    # Authors with a book that costs 20 or more. Alice has two such books.
    query = session.query(Author).join(Book).where(Book.price >= 20).order_by("name")
    # The join gives one row for each matching book, so Alice comes two times.
    assert [author.name for author in query] == ["Alice", "Alice", "Bob"]
    assert [author.name for author in query.distinct()] == ["Alice", "Bob"]


def test_max_of_a_date_column_returns_a_date(session):
    session.query(Book).where(Book.id == 1).update(released_on=datetime.date(2022, 3, 1))
    result = session.query(Book).aggregate(latest=Max(Book.released_on))
    assert result == {"latest": datetime.date(2022, 3, 1)}


def test_bulk_update(db, session):
    count = session.query(Book).where(Book.published == False).update(published=True, price=30)  # noqa: E712
    assert count == 1
    assert db.statements[-1] == dialect_sql(
        db, 'UPDATE "books" SET "published" = ?, "price" = ? WHERE "books"."published" = ?'
    )
    assert session.query(Book).where(Book.published == True).count() == 3  # noqa: E712


def test_bulk_update_does_not_change_instances_in_memory(session):
    book = session.get(Book, 1)
    session.query(Book).where(Book.id == 1).update(price=99.0)
    assert book.price == 20.0  # The instance in the identity map is stale.
    session.refresh(book)
    assert book.price == 99.0


def test_bulk_update_validates_the_values(session):
    with pytest.raises(ValidationError):
        session.query(Book).update(price="free")
    with pytest.raises(QueryError, match="primary key"):
        session.query(Book).update(id=5)
    with pytest.raises(QueryError, match="supports only where"):
        session.query(Book).join(Author).update(price=1.0)


def test_bulk_delete(session):
    assert session.query(Book).where(Book.price < 30).delete() == 2
    assert session.query(Book).count() == 1
    with pytest.raises(QueryError, match="supports only where"):
        session.query(Book).limit(1).delete()
