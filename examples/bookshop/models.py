"""The models of the bookshop example. Chapter 10 of the guide changes them."""

from mini_orm import FloatField, ForeignKey, Model, StringField


class Author(Model):
    __tablename__ = "authors"
    name = StringField(max_length=100, null=False)


class Book(Model):
    __tablename__ = "books"
    title = StringField(null=False)
    price = FloatField(default=0.0)
    author = ForeignKey(Author, related_name="books", on_delete="CASCADE")
