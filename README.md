# mini_orm

A small ORM for SQLite, PostgreSQL and MySQL, in pure Python.

I started this project to understand how Django and SQLAlchemy work inside:
how a Python class becomes a table, and how `Book.price > 10` becomes SQL.

```python
class Author(Model):
    __tablename__ = "authors"
    name = StringField(max_length=100, null=False)
```

Run the tests with `pytest` and the demo with `python examples/tour.py`.
More to come: queries, sessions, relations, migrations.
