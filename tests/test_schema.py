"""Tests for the schema statements and the dependency sort."""

import pytest

from mini_orm import CircularDependencyError, create_table_sql, drop_table_sql
from mini_orm.graph import sort_by_dependencies
from mini_orm.schema import sort_models

from .example_models import ALL_MODELS, Author, Book, Category, Country, Review


def test_create_table_for_a_model_with_a_foreign_key():
    assert create_table_sql(Book) == (
        'CREATE TABLE IF NOT EXISTS "books" (\n'
        '    "id" INTEGER PRIMARY KEY NOT NULL,\n'
        '    "title" TEXT NOT NULL,\n'
        '    "price" REAL,\n'
        '    "published" INTEGER,\n'
        '    "released_on" TEXT,\n'
        '    "author_id" INTEGER,\n'
        '    FOREIGN KEY ("author_id") REFERENCES "authors" ("id") ON DELETE CASCADE\n'
        ")"
    )


def test_create_table_with_unique_and_max_length():
    assert create_table_sql(Author) == (
        'CREATE TABLE IF NOT EXISTS "authors" (\n'
        '    "id" INTEGER PRIMARY KEY NOT NULL,\n'
        '    "name" VARCHAR(50) NOT NULL,\n'
        '    "email" VARCHAR(100) UNIQUE\n'
        ")"
    )


def test_create_table_with_a_text_primary_key():
    assert '"code" VARCHAR(2) PRIMARY KEY NOT NULL' in create_table_sql(Country)


def test_create_table_with_a_foreign_key_to_itself():
    assert 'FOREIGN KEY ("parent_id") REFERENCES "categories" ("id")' in create_table_sql(Category)


def test_drop_table():
    assert drop_table_sql(Book) == 'DROP TABLE IF EXISTS "books"'


def test_sort_models_puts_referenced_models_first():
    ordered = sort_models([Review, Book, Author])
    assert ordered == [Author, Book, Review]


def test_create_tables_and_drop_tables(empty_db):
    empty_db.create_tables(ALL_MODELS)
    empty_db.create_tables(ALL_MODELS)  # IF NOT EXISTS: a second call is safe.
    assert empty_db.table_names() == ["authors", "books", "categories", "countries", "order", "reviews"]
    assert empty_db.column_names("books") == ["id", "title", "price", "published", "released_on", "author_id"]
    empty_db.drop_tables(ALL_MODELS)
    assert empty_db.table_names() == []


def test_sort_by_dependencies_keeps_the_original_order_where_possible():
    dependencies = {"a": [], "b": ["a"], "c": [], "d": ["b"]}
    result = sort_by_dependencies(["d", "c", "b", "a"], lambda item: dependencies[item])
    assert result == ["a", "b", "d", "c"]


def test_sort_by_dependencies_ignores_unknown_items_and_self_references():
    result = sort_by_dependencies(["b", "a"], lambda item: ["a", "outside"] if item == "b" else ["a"])
    assert result == ["a", "b"]


def test_sort_by_dependencies_finds_a_circle():
    dependencies = {"a": ["b"], "b": ["c"], "c": ["a"]}
    with pytest.raises(CircularDependencyError):
        sort_by_dependencies(["a", "b", "c"], lambda item: dependencies[item])
