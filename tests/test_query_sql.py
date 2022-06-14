"""Tests for the SQL text that a Query makes. These tests do not use a database."""

import pytest

from mini_orm import Avg, Count, Query, QueryError, SessionError

from .example_models import Author, Book, Category, Order, Review

BOOK_COLUMNS = (
    '"books"."id", "books"."title", "books"."price", "books"."published", '
    '"books"."released_on", "books"."author_id"'
)
AUTHOR_COLUMNS = '"authors"."id", "authors"."name", "authors"."email"'


def test_select_all_columns():
    assert Query(Book).to_sql() == (f'SELECT {BOOK_COLUMNS} FROM "books"', [])


def test_where_with_two_conditions_and_two_calls():
    query = Query(Book).where(Book.price > 10, Book.published == True)  # noqa: E712
    query = query.where(Book.title.like("S%"))
    text, params = query.to_sql()
    assert text == (
        f'SELECT {BOOK_COLUMNS} FROM "books" '
        'WHERE (("books"."price" > ? AND "books"."published" = ?) AND "books"."title" LIKE ?)'
    )
    assert params == [10.0, 1, "S%"]


def test_builder_methods_do_not_change_the_original_query():
    base = Query(Book)
    filtered = base.where(Book.price > 10)
    assert base.to_sql() == (f'SELECT {BOOK_COLUMNS} FROM "books"', [])
    assert filtered is not base
    assert "WHERE" in filtered.to_sql()[0]


def test_order_by_accepts_fields_orderings_and_names():
    query = Query(Book).order_by(Book.price.desc(), "title", "-id")
    assert query.to_sql()[0].endswith('ORDER BY "books"."price" DESC, "books"."title" ASC, "books"."id" DESC')


def test_order_by_replaces_the_earlier_order():
    query = Query(Book).order_by("title").order_by("price")
    assert query.to_sql()[0].endswith('ORDER BY "books"."price" ASC')


def test_limit_and_offset():
    assert Query(Book).limit(5).to_sql()[0].endswith("LIMIT ?")
    assert Query(Book).limit(5).offset(10).to_sql() == (
        f'SELECT {BOOK_COLUMNS} FROM "books" LIMIT ? OFFSET ?',
        [5, 10],
    )
    # SQLite needs a LIMIT before an OFFSET. -1 means "no limit".
    assert Query(Book).offset(10).to_sql() == (
        f'SELECT {BOOK_COLUMNS} FROM "books" LIMIT -1 OFFSET ?',
        [10],
    )


@pytest.mark.parametrize("wrong_value", [-1, 1.5, "3", True])
def test_limit_validation(wrong_value):
    with pytest.raises(QueryError):
        Query(Book).limit(wrong_value)


def test_distinct():
    assert Query(Author).distinct().to_sql()[0].startswith("SELECT DISTINCT ")


def test_join_many_to_one_finds_the_foreign_key():
    text, params = Query(Book).join(Author).where(Author.name == "Alice").to_sql()
    assert text == (
        f'SELECT {BOOK_COLUMNS} FROM "books" '
        'INNER JOIN "authors" ON "books"."author_id" = "authors"."id" '
        'WHERE "authors"."name" = ?'
    )
    assert params == ["Alice"]


def test_join_one_to_many_finds_the_foreign_key():
    text, _ = Query(Author).join(Book).to_sql()
    assert 'INNER JOIN "books" ON "books"."author_id" = "authors"."id"' in text


def test_join_chain():
    text, _ = Query(Review).join(Book).join(Author).to_sql()
    assert (
        'INNER JOIN "books" ON "reviews"."book_id" = "books"."id" '
        'INNER JOIN "authors" ON "books"."author_id" = "authors"."id"'
    ) in text


def test_join_with_an_explicit_condition():
    text, _ = Query(Book).join(Author, on=Book.author == Author.id).to_sql()
    assert 'INNER JOIN "authors" ON "books"."author_id" = "authors"."id"' in text


def test_join_errors():
    with pytest.raises(QueryError, match="No ForeignKey connects"):
        Query(Author).join(Category)
    with pytest.raises(QueryError, match="already in the query"):
        Query(Book).join(Author).join(Author)
    with pytest.raises(QueryError, match="already in the query"):
        Query(Category).join(Category)


def test_select_related_adds_a_left_join_and_the_related_columns():
    text, _ = Query(Book).select_related("author").to_sql()
    assert text == (
        f'SELECT {BOOK_COLUMNS}, {AUTHOR_COLUMNS} FROM "books" '
        'LEFT JOIN "authors" ON "books"."author_id" = "authors"."id"'
    )


def test_select_related_errors():
    with pytest.raises(QueryError, match="not one"):
        Query(Book).select_related("title")
    with pytest.raises(QueryError, match="has no field"):
        Query(Book).select_related("publisher")
    with pytest.raises(QueryError, match="Use prefetch_related"):
        Query(Category).select_related("parent")


def test_prefetch_related_checks_the_name():
    Query(Author).prefetch_related("books")
    Query(Book).prefetch_related("author", "reviews")
    with pytest.raises(QueryError, match="has no relationship"):
        Query(Author).prefetch_related("reviews")


def test_group_by_having_and_values():
    query = (
        Query(Book)
        .group_by(Book.author)
        .having(Count() > 1)
        .values(Book.author, books=Count(), average_price=Avg(Book.price))
    )
    assert query.to_sql() == (
        'SELECT "books"."author_id" AS "author", COUNT(*) AS "books", '
        'AVG("books"."price") AS "average_price" FROM "books" '
        'GROUP BY "books"."author_id" HAVING COUNT(*) > ?',
        [1],
    )


def test_values_errors():
    with pytest.raises(QueryError, match="same key"):
        Query(Book).values(Book.title, title=Count())
    with pytest.raises(QueryError, match="needs a field or an aggregate"):
        Query(Book).values(total=5)
    with pytest.raises(QueryError, match="cannot be used with values"):
        Query(Book).values(Book.title).select_related("author")


def test_reserved_words_are_quoted():
    text, params = Query(Order).where(Order.group == "a").to_sql()
    assert text == 'SELECT "order"."id", "order"."group" FROM "order" WHERE "order"."group" = ?'


def test_a_query_without_a_session_cannot_run():
    with pytest.raises(SessionError, match="no session"):
        Query(Book).all()


def test_repr_shows_the_sql():
    assert repr(Query(Author).where(Author.id == 1)) == (
        f'<Query Author: SELECT {AUTHOR_COLUMNS} FROM "authors" WHERE "authors"."id" = ? [1]>'
    )
