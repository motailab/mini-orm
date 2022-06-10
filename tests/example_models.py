"""The models that the tests use: a small book shop."""

from mini_orm import (
    BooleanField,
    DateField,
    DateTimeField,
    FloatField,
    ForeignKey,
    IntegerField,
    Model,
    StringField,
)


class Author(Model):
    __tablename__ = "authors"
    name = StringField(max_length=50, null=False)
    email = StringField(max_length=100, unique=True)


class Book(Model):
    __tablename__ = "books"
    title = StringField(null=False)
    price = FloatField(default=0.0)
    published = BooleanField(default=False)
    released_on = DateField()
    author = ForeignKey(Author, related_name="books", on_delete="CASCADE")


class Review(Model):
    __tablename__ = "reviews"
    book = ForeignKey(Book, related_name="reviews", on_delete="CASCADE")
    rating = IntegerField(null=False)
    created_at = DateTimeField()


class Category(Model):
    """A model with a foreign key to itself (a tree)."""

    __tablename__ = "categories"
    name = StringField(null=False)
    parent = ForeignKey("self", related_name="children")


class Country(Model):
    """A model with a text primary key."""

    __tablename__ = "countries"
    code = StringField(primary_key=True, max_length=2)
    name = StringField(null=False)


class Order(Model):
    """A model whose table name and column name are SQL reserved words."""

    __tablename__ = "order"
    group = StringField()


ALL_MODELS = [Author, Book, Review, Category, Country, Order]
