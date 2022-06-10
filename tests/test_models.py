"""Tests for the ModelMeta metaclass and the Model class."""

import datetime

import pytest

from mini_orm import (
    DateTimeField,
    ForeignKey,
    IntegerField,
    Model,
    ModelDefinitionError,
    Query,
    QueryError,
    SQLiteDialect,
    StringField,
    ValidationError,
)
from mini_orm.models import ModelMetadata

from .example_models import Author, Book, Category, Country


def test_meta_has_the_table_information():
    meta = Book._meta
    assert isinstance(meta, ModelMetadata)
    assert meta.table_name == "books"
    assert list(meta.fields) == ["id", "title", "price", "published", "released_on", "author"]
    assert meta.primary_key is meta.fields["id"]
    assert len(meta.foreign_keys) == 1
    assert meta.foreign_keys[0] is Book.author


def test_default_table_name_is_the_lowercase_class_name():
    class Customer(Model):
        name = StringField()

    assert Customer._meta.table_name == "customer"


def test_metaclass_adds_an_id_primary_key_as_the_first_column():
    class Tag(Model):
        label = StringField()

    assert list(Tag._meta.fields) == ["id", "label"]
    assert isinstance(Tag.id, IntegerField)
    assert Tag.id.primary_key


def test_custom_primary_key_replaces_the_id():
    assert list(Country._meta.fields) == ["code", "name"]
    assert Country._meta.primary_key is Country.code


def test_two_primary_keys_are_not_allowed():
    with pytest.raises(ModelDefinitionError, match="more than one primary key"):

        class Broken(Model):
            a = IntegerField(primary_key=True)
            b = IntegerField(primary_key=True)


def test_abstract_model_shares_fields_but_each_child_gets_copies():
    class Timestamped(Model):
        __abstract__ = True
        created_at = DateTimeField()

    class Post(Timestamped):
        __tablename__ = "posts"
        title = StringField()

    class Note(Timestamped):
        __tablename__ = "notes"

    assert list(Post._meta.fields) == ["id", "created_at", "title"]
    # Each child has its own field object, which knows its own model.
    assert Post.created_at is not Note.created_at
    assert Post.created_at.model is Post
    assert Note.created_at.model is Note
    assert Post.created_at.to_sql(SQLiteDialect())[0] == '"posts"."created_at"'
    assert Note.created_at.to_sql(SQLiteDialect())[0] == '"notes"."created_at"'


def test_abstract_model_cannot_have_instances_or_queries():
    class Base(Model):
        __abstract__ = True

    with pytest.raises(TypeError, match="abstract"):
        Base()
    with pytest.raises(QueryError):
        Query(Base)


def test_unknown_keyword_argument():
    with pytest.raises(TypeError, match="Author has no field: age"):
        Author(name="Alice", age=30)


def test_init_validates_the_values():
    with pytest.raises(ValidationError):
        Author(name=123)


def test_repr_shows_raw_values_and_runs_no_query():
    author = Author(name="Alice")
    book = Book(title="SQL", author=author)
    # The book is not in a session, so a query would fail. repr() must work.
    assert repr(book) == (
        "Book(id=None, title='SQL', price=0.0, published=False, released_on=None, author=None)"
    )


def test_pk_property():
    assert Author(name="Alice").pk is None
    assert Country(code="DE", name="Germany").pk == "DE"


def test_from_db_does_not_validate_and_converts_types():
    book = Book._from_db(
        {
            "id": 7,
            "title": "SQL",
            "price": 10.0,
            "published": 1,
            "released_on": "2022-05-01",
            "author": 3,
        }
    )
    assert book.published is True
    assert book.released_on == datetime.date(2022, 5, 1)
    assert book.author_id == 3
    assert not book._state.persisted  # The session sets the snapshot, not _from_db.


def test_foreign_key_column_name_and_id_attribute():
    assert Book.author.column == "author_id"
    book = Book(title="SQL", author=5)
    assert book.author_id == 5
    book.author_id = 6
    assert book.__dict__["author"] == 6


def test_foreign_key_to_self():
    assert Category.parent.target_model is Category
    assert Category._meta.reverse_relations["children"].foreign_key is Category.parent


def test_foreign_key_needs_a_model_class_or_self():
    with pytest.raises(ModelDefinitionError, match="Define the target model"):
        ForeignKey("Author")


def test_foreign_key_on_delete_must_be_valid():
    with pytest.raises(ModelDefinitionError, match="on_delete must be one of"):
        ForeignKey(Author, on_delete="EXPLODE")
    with pytest.raises(ModelDefinitionError, match="needs null=True"):
        ForeignKey(Author, on_delete="SET NULL", null=False)


def test_foreign_key_validates_the_type():
    with pytest.raises(ValidationError, match="needs an instance of Author or a primary key value"):
        Book(title="SQL", author="Alice")
    with pytest.raises(ValidationError):
        Book(title="SQL", author=Country(code="DE", name="Germany"))


def test_reverse_relation_is_added_to_the_target_model():
    assert "books" in Author._meta.reverse_relations
    assert Author._meta.get_relation("books").foreign_key is Book.author


def test_related_name_cannot_hide_an_attribute():
    with pytest.raises(ModelDefinitionError, match="already has an attribute 'books'"):

        class Comic(Model):
            author = ForeignKey(Author, related_name="books")


def test_get_field_and_get_relation_errors():
    with pytest.raises(QueryError, match="Book has no field 'isbn'"):
        Book._meta.get_field("isbn")
    with pytest.raises(QueryError, match="Book has no relationship 'title'"):
        Book._meta.get_relation("title")
