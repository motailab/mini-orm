"""A tour of mini_orm. Run it and read the SQL that each step prints.

    python examples/tour.py                     # SQLite in memory
    python examples/tour.py "postgresql://..."  # an empty PostgreSQL database
    python examples/tour.py "mysql://..."       # an empty MySQL database

At the end, the tour drops its tables, so the database is as it was before the tour.
"""

import datetime
import sys

from mini_orm import (
    Avg,
    BooleanField,
    Count,
    Database,
    DateTimeField,
    FloatField,
    ForeignKey,
    Model,
    Session,
    StringField,
    create_table_sql,
)

# ---------------------------------------------------------------------------
# 1. Models: each class is a table, each field is a column.
# ---------------------------------------------------------------------------


class Author(Model):
    __tablename__ = "authors"
    name = StringField(max_length=100, null=False)
    email = StringField(max_length=100, unique=True)


class Book(Model):
    __tablename__ = "books"
    title = StringField(null=False)
    price = FloatField(default=0.0)
    published = BooleanField(default=False)
    added_at = DateTimeField(default=datetime.datetime.now)  # A function: called for each book.
    author = ForeignKey(Author, related_name="books", on_delete="CASCADE")


class Category(Model):
    __tablename__ = "categories"
    name = StringField(null=False)
    parent = ForeignKey("self", related_name="children")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


MODELS = [Book, Author, Category]


def main(url: str = ":memory:") -> None:
    db = Database(url, echo=True)
    existing = {model._meta.table_name for model in MODELS} & set(db.table_names())
    if existing:
        print(f"The database has tables of the tour already: {sorted(existing)}. Use an empty database.")
        return

    section(f"2. Schema: the ORM makes CREATE TABLE from the model ({db.dialect.name})")
    print(create_table_sql(Book, db.dialect))
    db.create_tables(MODELS)  # Sorted: authors before books.

    section("3. Unit of work: add() runs no SQL, commit() runs it in the right order")
    with Session(db) as session:
        mutaleb = Author(name="Mutaleb", email="mutaleb@example.com")
        jannat = Author(name="Jannat", email="jannat@example.com")
        # The books come first in the list, but the authors are inserted first,
        # because each book needs the primary key of its author.
        session.add_all(
            [
                Book(title="SQL Basics", price=20, published=True, author=mutaleb),
                Book(title="Advanced SQL", price=35, published=True, author=mutaleb),
                Book(title="Python Tricks", price=25, author=jannat),
                mutaleb,
                jannat,
            ]
        )
        print("-- add_all() is done. No SQL yet. Now commit():")
        session.commit()
        print(f"-- mutaleb.id is now {mutaleb.id}")

    section("4. Identity map: one object for each row")
    with Session(db) as session:
        first = session.get(Book, 1)
        print("-- The second get() runs no SQL:")
        second = session.get(Book, 1)
        print(f"-- first is second: {first is second}")

    section("5. Queries: Python operators become SQL with parameters")
    with Session(db) as session:
        query = (
            session.query(Book)
            .where((Book.price >= 20) & Book.title.like("%SQL%"))
            .order_by(Book.price.desc())
            .limit(10)
        )
        print(f"-- to_sql() runs nothing: {query.to_sql()}")
        for book in query:
            print(f"-- {book.title}: {book.price}")

    section("6. Dirty checking: the UPDATE has only the changed column")
    with Session(db) as session:
        book = session.get(Book, 3)
        book.price = 30.0
        session.commit()

    section("7a. Relationships: the N+1 problem (lazy loading in a loop)")
    with Session(db) as session:
        for book in session.query(Book).all():
            print(f"-- {book.title} by {book.author.name}")  # One SELECT for each new author.

    section("7b. select_related(): one SELECT with a LEFT JOIN")
    with Session(db) as session:
        for book in session.query(Book).select_related("author").all():
            print(f"-- {book.title} by {book.author.name}")

    section("7c. prefetch_related(): one more SELECT with IN (...)")
    with Session(db) as session:
        for author in session.query(Author).prefetch_related("books").all():
            print(f"-- {author.name}: {[book.title for book in author.books]}")

    section("8. Aggregates: GROUP BY, HAVING and values()")
    with Session(db) as session:
        rows = (
            session.query(Book)
            .group_by(Book.author)
            .having(Count() >= 1)
            .values(Book.author, books=Count(), average_price=Avg(Book.price))
            .all()
        )
        print(f"-- {rows}")
        print(f"-- {session.query(Book).aggregate(total=Count(), average=Avg(Book.price))}")

    section("9. Transactions: rollback() undoes the changes")
    with Session(db) as session:
        session.add(Author(name="Temporary"))
        session.flush()  # The INSERT runs, but there is no COMMIT.
        print(f"-- Authors in the transaction: {session.query(Author).count()}")
        session.rollback()
        print(f"-- Authors after the rollback: {session.query(Author).count()}")

    section("10. A tree: a ForeignKey to the same model")
    with Session(db) as session:
        root = Category(name="Programming")
        session.add_all([Category(name="Python", parent=root), Category(name="SQL", parent=root), root])
        session.commit()
        print(f"-- children of {root.name}: {[child.name for child in root.children]}")

    db.echo = False
    db.drop_tables(MODELS)  # Leave the database as it was before the tour.
    db.close()


if __name__ == "__main__":
    main(*sys.argv[1:2])
