"""Tests for the expression tree: Python operators become SQL text and parameters."""

import datetime

import pytest

from mini_orm import Avg, Count, Max, Min, MySQLDialect, QueryError, SQLiteDialect, Sum, ValidationError

from .example_models import Author, Book, Review


def sql(expression):
    """Return the (text, params) pair of an expression, for SQLite."""
    return expression.to_sql(SQLiteDialect())


def test_a_field_on_the_class_is_a_column_reference():
    assert sql(Book.price) == ('"books"."price"', [])


@pytest.mark.parametrize(
    ("condition", "expected_text"),
    [
        (Book.price == 10, '"books"."price" = ?'),
        (Book.price != 10, '"books"."price" <> ?'),
        (Book.price < 10, '"books"."price" < ?'),
        (Book.price <= 10, '"books"."price" <= ?'),
        (Book.price > 10, '"books"."price" > ?'),
        (Book.price >= 10, '"books"."price" >= ?'),
    ],
)
def test_comparison_operators(condition, expected_text):
    assert sql(condition) == (expected_text, [10.0])


def test_the_value_never_goes_into_the_sql_text():
    dangerous = "x'; DROP TABLE books; --"
    text, params = sql(Book.title == dangerous)
    assert text == '"books"."title" = ?'
    assert params == [dangerous]


def test_reflected_comparison():
    # Python calls Book.price.__gt__(10) when int.__lt__ cannot compare.
    assert sql(10 < Book.price) == ('"books"."price" > ?', [10.0])  # noqa: SIM300


def test_compare_with_none_makes_is_null():
    assert sql(Book.released_on == None) == ('"books"."released_on" IS NULL', [])  # noqa: E711
    assert sql(Book.released_on != None) == ('"books"."released_on" IS NOT NULL', [])  # noqa: E711
    assert sql(Book.released_on.is_null()) == ('"books"."released_on" IS NULL', [])
    assert sql(Book.released_on.is_not_null()) == ('"books"."released_on" IS NOT NULL', [])
    with pytest.raises(QueryError, match="Cannot use None"):
        Book.price > None  # noqa: B015


def test_and_or_not():
    condition = (Book.price > 10) & ((Book.title == "A") | ~(Book.published == True))  # noqa: E712
    assert sql(condition) == (
        '("books"."price" > ? AND ("books"."title" = ? OR NOT ("books"."published" = ?)))',
        [10.0, "A", True],
    )


def test_python_and_keyword_raises_an_error():
    with pytest.raises(TypeError, match="Use & for AND"):
        (Book.price > 10) and (Book.price < 20)  # noqa: B018


def test_ampersand_with_a_non_condition_raises_type_error():
    with pytest.raises(TypeError):
        (Book.price > 10) & True


def test_in_list():
    assert sql(Book.id.in_([1, 2, 3])) == ('"books"."id" IN (?, ?, ?)', [1, 2, 3])
    # PostgreSQL and MySQL refuse "IN ()", so an empty list is a condition that is always false.
    assert sql(Book.id.in_([])) == ("1 = 0", [])
    with pytest.raises(QueryError, match="needs a list"):
        Book.title.in_("abc")


def test_like_and_between():
    assert sql(Book.title.like("SQL%")) == ('"books"."title" LIKE ?', ["SQL%"])
    assert sql(Book.price.between(10, 20)) == ('"books"."price" BETWEEN ? AND ?', [10.0, 20.0])


def test_values_stay_standard_python_values():
    # The dialect adapts the values when the Database runs the statement.
    assert sql(Book.published == True)[1] == [True]  # noqa: E712
    assert sql(Book.released_on > datetime.date(2022, 1, 31))[1] == [datetime.date(2022, 1, 31)]
    created = datetime.datetime(2022, 1, 31, 8, 0)
    assert sql(Review.created_at < created)[1] == [created]
    assert sql(Book.price > 10)[1] == [10.0]  # The field converts an int to a float.


def test_values_are_validated():
    with pytest.raises(ValidationError):
        Book.price > "ten"  # noqa: B015


def test_foreign_key_compared_with_an_instance_uses_its_primary_key():
    author = Author(name="Alice")
    author.id = 4
    assert sql(Book.author == author) == ('"books"."author_id" = ?', [4])
    assert sql(Book.author == 4) == ('"books"."author_id" = ?', [4])


def test_field_compared_with_a_field_makes_no_parameter():
    assert sql(Book.author == Author.id) == ('"books"."author_id" = "authors"."id"', [])


def test_aggregates():
    assert sql(Count()) == ("COUNT(*)", [])
    assert sql(Count(Book.price)) == ('COUNT("books"."price")', [])
    assert sql(Count(Book.author, distinct=True)) == ('COUNT(DISTINCT "books"."author_id")', [])
    assert sql(Sum(Book.price)) == ('SUM("books"."price")', [])
    assert sql(Avg(Book.price)) == ('AVG("books"."price")', [])
    assert sql(Min(Book.price)) == ('MIN("books"."price")', [])
    assert sql(Max(Book.price)) == ('MAX("books"."price")', [])
    with pytest.raises(QueryError, match="needs a field"):
        Sum("price")


def test_an_aggregate_can_be_compared():
    assert sql(Count() > 1) == ("COUNT(*) > ?", [1])


def test_min_and_max_convert_the_result_to_the_field_type():
    assert Max(Book.released_on).from_db("2022-05-01") == datetime.date(2022, 5, 1)
    assert Count().from_db(3) == 3


def test_ordering():
    assert sql(Book.price.desc()) == ('"books"."price" DESC', [])
    assert sql(Book.price.asc()) == ('"books"."price" ASC', [])


def test_quote_name():
    assert SQLiteDialect().quote_name("order") == '"order"'
    assert SQLiteDialect().quote_name('my"table') == '"my""table"'
    assert MySQLDialect().quote_name("order") == "`order`"
    assert MySQLDialect().quote_name("my`table") == "`my``table`"
