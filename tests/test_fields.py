"""Tests for the field classes: validation, defaults and conversion."""

import datetime

import pytest

from mini_orm import (
    BooleanField,
    DateField,
    DateTimeField,
    FloatField,
    IntegerField,
    Model,
    MySQLDialect,
    PostgreSQLDialect,
    SQLiteDialect,
    StringField,
    ValidationError,
)
from mini_orm.schema import Column


class Product(Model):
    __tablename__ = "products"
    name = StringField(max_length=10, null=False)
    stock = IntegerField(default=0)
    price = FloatField()
    active = BooleanField(default=True)
    created_at = DateTimeField(default=lambda: datetime.datetime(2022, 1, 1, 12, 0))
    available_on = DateField()


def test_class_access_returns_the_field():
    assert isinstance(Product.stock, IntegerField)
    assert Product.stock.name == "stock"
    assert Product.stock.model is Product


def test_instance_access_returns_the_value():
    product = Product(name="Pen", stock=3)
    assert product.stock == 3
    assert product.__dict__["stock"] == 3


def test_default_values_and_callable_defaults():
    product = Product(name="Pen")
    assert product.stock == 0
    assert product.active is True
    assert product.created_at == datetime.datetime(2022, 1, 1, 12, 0)
    assert product.price is None


def test_integer_field_rejects_bool_text_and_float():
    product = Product(name="Pen")
    for wrong_value in (True, "3", 3.5):
        with pytest.raises(ValidationError, match="Product.stock must be int"):
            product.stock = wrong_value


def test_float_field_accepts_an_int_and_stores_a_float():
    product = Product(name="Pen", price=10)
    assert product.price == 10.0
    assert isinstance(product.price, float)


def test_float_field_rejects_bool_and_text():
    with pytest.raises(ValidationError):
        Product(name="Pen", price=True)
    with pytest.raises(ValidationError):
        Product(name="Pen", price="10")


def test_string_field_max_length():
    with pytest.raises(ValidationError, match="can have 10 characters at most, not 11"):
        Product(name="x" * 11)


def test_null_false_rejects_none():
    with pytest.raises(ValidationError, match="Product.name cannot be None"):
        Product()
    product = Product(name="Pen")
    with pytest.raises(ValidationError):
        product.name = None


def test_null_true_accepts_none():
    product = Product(name="Pen", stock=None)
    assert product.stock is None


def test_set_none_does_not_use_the_default():
    product = Product(name="Pen", stock=5)
    product.stock = None
    assert product.stock is None


def test_boolean_field_conversion():
    field = Product.active
    assert field.to_db(True) is True  # A standard value. The driver adapts it.
    assert field.from_db(1) is True  # SQLite and MySQL give 1 or 0.
    assert field.from_db(0) is False
    assert field.from_db(True) is True  # PostgreSQL gives a bool.
    assert field.to_db(None) is None


def test_datetime_field_conversion():
    value = datetime.datetime(2022, 5, 1, 14, 30, 15)
    assert Product.created_at.to_db(value) == value
    assert Product.created_at.from_db("2022-05-01T14:30:15") == value  # SQLite gives text.
    assert Product.created_at.from_db(value) == value  # PostgreSQL and MySQL give a datetime.
    assert SQLiteDialect().adapt_value(value) == "2022-05-01T14:30:15"
    assert PostgreSQLDialect().adapt_value(value) == value


def test_date_field_conversion_and_validation():
    day = datetime.date(2022, 5, 1)
    assert Product.available_on.to_db(day) == day
    assert Product.available_on.from_db("2022-05-01") == day
    assert Product.available_on.from_db(day) == day
    assert SQLiteDialect().adapt_value(day) == "2022-05-01"
    with pytest.raises(ValidationError, match="must be date, not datetime"):
        Product(name="Pen", available_on=datetime.datetime(2022, 5, 1))


def test_column_types_and_sql_types():
    def sql_type(field, dialect):
        return dialect.column_type(Column.from_field(field))

    assert [field.column_type for field in (Product.stock, Product.price, Product.active)] == [
        "integer",
        "float",
        "boolean",
    ]
    sqlite, postgresql, mysql = SQLiteDialect(), PostgreSQLDialect(), MySQLDialect()
    assert sql_type(Product.name, sqlite) == "VARCHAR(10)"
    assert sql_type(Product.price, sqlite) == "REAL"
    assert sql_type(Product.price, postgresql) == "DOUBLE PRECISION"
    assert sql_type(Product.price, mysql) == "DOUBLE"
    assert sql_type(Product.active, sqlite) == "INTEGER"
    assert sql_type(Product.active, postgresql) == "BOOLEAN"
    assert sql_type(Product.created_at, sqlite) == "TEXT"
    assert sql_type(Product.created_at, postgresql) == "TIMESTAMP"
    assert sql_type(Product.created_at, mysql) == "DATETIME(6)"


def test_fields_can_be_dictionary_keys():
    # Field defines __eq__, so it must restore __hash__ (see expressions.Operand).
    columns = {Product.name: "a", Product.stock: "b"}
    assert columns[Product.stock] == "b"
    assert Product.name in {Product.name}
