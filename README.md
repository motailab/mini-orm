# mini_orm

A lightweight, zero-dependency Object-Relational Mapper (ORM) and schema migration engine implemented in pure Python from first principles.

> **Engineering Context**: This project is **not** a production replacement for SQLAlchemy or Django ORM. In production systems, use mature, battle-tested libraries. `mini_orm` was created to answer a fundamental technical question: *How do ORMs actually work under the hood?* It demystifies the translation of Python semantics into relational algebra using advanced Python language features: metaclasses, descriptors, Abstract Syntax Tree (AST) compilation, topological sorting, and schema diffing.

---

## The Origin: Demystifying the Magic

Modern ORMs power thousands of production backends. Yet for many software engineers, they function like magical black boxes:

* How does a Python comparison like `Book.price >= 10` avoid returning a boolean and instead produce a parameterized SQL `WHERE` clause?
* How does an ORM track object changes without manual `save()` calls on every property?
* How does a session calculate the exact order to insert objects without breaking foreign-key constraints?
* How do migration engines compute schema diffs and mutate database tables on engines like SQLite that historically prohibited `ALTER TABLE DROP COLUMN`?

`mini_orm` answers each of these questions directly in code. Every component is built with zero external runtime dependencies using only the Python standard library.

---

## System Architecture

`mini_orm` consists of five decoupled subsystems:

```
[ Python Model Definitions ] ──( Metaclass & Descriptors )──> [ Schema Metadata ]
             │
             ▼
[ Query Builder API ] ───────( Operator Overloading )───────> [ Expression AST ]
             │                                                        │
             ▼                                                        ▼
[ Session / Unit of Work ] ──( Snapshot Diff & Topo Sort )──> [ Dialect Compiler ]
             │                                                        │
             ▼                                                        ▼
[ DB Driver (SQLite / PG / MySQL) ] <──────────────────────── [ Parameterized SQL ]
```

---

## 1. Metaprogramming & Field Descriptors

When you declare a model, Python registers table metadata at import time before any instance is created:

```python
from mini_orm import Model, StringField, FloatField, ForeignKey

class Author(Model):
    __tablename__ = "authors"
    name = StringField(max_length=100, null=False)

class Book(Model):
    __tablename__ = "books"
    title = StringField(null=False)
    price = FloatField(default=0.0)
    author = ForeignKey(Author, related_name="books", on_delete="CASCADE")

# Basic persistence:
with Session(db) as session:
    mutaleb = Author(name="Mutaleb")
    jannat = Author(name="Jannat")
    session.add_all([
        mutaleb,
        jannat,
        Book(title="SQL Basics", price=20.0, author=mutaleb),
        Book(title="Python Tricks", price=25.0, author=jannat),
    ])
    session.commit()
```

### How It Works Under the Hood
1. **Class-Time Collection (`mini_orm/models.py`)**: `ModelMetaclass` intercepts class creation. It inspects class attributes, collects all `Field` instances, registers foreign-key back-references, and configures an automatic `id` primary key.
2. **Descriptor Protocol (`mini_orm/fields.py`)**: Every field implements `__get__` and `__set__`. Attribute assignments trigger immediate type validation and constraint checks (`null=False`, `max_length`, `unique`). Values live directly inside the instance's `__dict__`, ensuring minimal memory overhead.

---

## 2. Expressions AST & Dialect Compiler

To prevent SQL injection by design, `mini_orm` overrides Python dunder operators (`__eq__`, `__ge__`, `__and__`, `__invert__`) to construct an Expression AST instead of evaluating truth values:

```python
with Session(db) as session:
    books = (
        session.query(Book)
        .where((Book.price >= 10.0) & Book.title.like("SQL%"))
        .order_by(Book.price.desc())
        .limit(10)
        .all()
    )
```

### The AST Translation Pipeline
1. `Book.price >= 10.0` produces a `BinaryOp(left=ColumnRef('price'), op='>=', right=Literal(10.0))`.
2. The `&` operator combines nodes into an `AndOp` node.
3. The visitor compiler (`mini_orm/compiler.py`) walks the AST and emits parameterized SQL.

```sql
-- SQLite Output:
SELECT "books"."id", "books"."title", "books"."price", "books"."author_id"
FROM "books"
WHERE ("books"."price" >= ? AND "books"."title" LIKE ?)
ORDER BY "books"."price" DESC
LIMIT ?
-- Parameters: [10.0, 'SQL%', 10]
```

PostgreSQL and MySQL automatically receive `%s` placeholders, and MySQL uses backticks (`` `books` ``) for identifier escaping. SQL injection is impossible because user values never concatenate into the SQL string.

---

## 3. Unit of Work, Identity Map & Topological Sorting

`mini_orm` implements Martin Fowler's **Unit of Work** and **Identity Map** patterns (`mini_orm/session.py`):

### The Identity Map Guarantee
The session guarantees that each database row maps to exactly one Python instance in memory:

```python
with Session(db) as session:
    book1 = session.get(Book, 1)
    book2 = session.get(Book, 1)
    assert book1 is book2  # Same memory reference: runs zero extra queries!
```

### Snapshot-Based Dirty Checking
When an entity loads, the session saves a pristine snapshot of its fields. On `session.commit()`:
1. The session compares current values against original snapshots.
2. If only one field changed, the session generates an `UPDATE` targeting only that column:
   ```sql
   UPDATE "books" SET "price" = ? WHERE "books"."id" = ?  -- Parameters: [25.0, 1]
   ```
3. If no fields changed, the session runs zero SQL queries.

### Dependency Resolution (Topological Sort)
When persisting connected records added out of order, naive inserts fail with foreign-key errors. `mini_orm` constructs a dependency graph and executes a **topological sort** (`mini_orm/graph.py`):

```python
with Session(db) as session:
    author = Author(name="Abdul")
    book = Book(title="Database Fundamentals", price=40.0, author=author)
    session.add_all([book, author])  # Added child before parent!
    session.commit()
```

The resolver inserts `Author` first, retrieves the database-generated `id`, assigns `book.author_id`, and then inserts `Book`.

---

## 4. Solving the N+1 Query Problem

Lazy loading relations inside a loop triggers the notorious N+1 query problem. `mini_orm` provides two eager-loading strategies (`mini_orm/relations.py`):

```python
# Problem: 1 query for books + N queries for authors (N+1 queries)
for book in session.query(Book).all():
    print(book.title, book.author.name)  # e.g., "SQL Basics by Mutaleb"

# Solution 1: select_related() -> Exactly 1 query using SQL LEFT JOIN
for book in session.query(Book).select_related("author").all():
    print(book.title, book.author.name)

# Solution 2: prefetch_related() -> Exactly 2 queries using IN (...)
for author in session.query(Author).prefetch_related("books").all():
    print(author.name, [b.title for b in author.books])  # e.g., "Jannat: ['Python Tricks']"
```

* Use `select_related()` for single-valued relationships (`ForeignKey` to parent).
* Use `prefetch_related()` for multi-valued relationships (back-references and trees) to avoid giant cartesian products.

---

## 5. How Schema Migrations Work

`mini_orm` features a complete, standalone schema migration engine (`mini_orm/migrations/`).

### The Migration Lifecycle

```
[ Current Models ]           [ Historical Migrations ]
        │                               │
        └───────────────┬───────────────┘
                        ▼
             [ 1. Autodetector ]  ->  Calculates State Diff
                        │
                        ▼
             [ 2. File Writer ]   ->  Generates 0002_add_field.py
                        │
                        ▼
             [ 3. CLI / Executor ]
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
[ Postgres / MySQL ]           [ SQLite Schema Editor ]
Native ALTER TABLE DDL         5-Step Table Rebuild Algorithm
```

### 1. State Autodetection (`autodetector.py`)
The autodetector loads all existing migration files in order and builds a virtual `ProjectState`. It then compares this virtual state with your live Python `Model` classes. It detects:
* New tables (`CreateTable`) and deleted tables (`DropTable`).
* Added columns (`AddColumn`) and removed columns (`DropColumn`).
* Altered column types, nullability, and default values (`AlterColumn`).
* Renamed columns (`RenameColumn`).

### 2. Standalone Migration Files (`writer.py`)
The migration file is human-readable, deterministic Python code:

```python
# migrations/0002_add_stock.py
from mini_orm import migrations

class Migration(migrations.Migration):
    dependencies = ['0001_initial']
    operations = [
        migrations.AddColumn('books', migrations.Column('stock', 'integer', null=False), default=0),
    ]
```

### 3. Bidirectional Execution (`executor.py` & `recorder.py`)
* The recorder maintains an internal table named `mini_orm_migrations` in the target database.
* The executor runs operations inside a database transaction.
* If a migration fails midway, the transaction rolls back cleanly.
* Backward migrations (`migrate 0001` or `migrate zero`) undo operations in reverse order without data loss.

### 4. SQLite Table Rebuild Engine
PostgreSQL and MySQL support standard `ALTER TABLE ADD/DROP COLUMN` statements. However, SQLite historically prevented altering column definitions or dropping columns.

`mini_orm`'s `SqliteSchemaEditor` automatically performs a transparent 5-step table recreation:
1. `CREATE TABLE "new__books" (...)` with updated columns and foreign-key constraints.
2. `INSERT INTO "new__books" SELECT <mapped_columns> FROM "books"`.
3. `DROP TABLE "books"`.
4. `ALTER TABLE "new__books" RENAME TO "books"`.
5. Run `PRAGMA foreign_key_check` to ensure no relational constraints were broken.

---

## Multi-Database Support & Testing

`mini_orm` supports three database backends using unified connection URLs:

| Database | Connection URL Example | Driver Required |
| :--- | :--- | :--- |
| **SQLite** | `shop.db`, `:memory:`, or `sqlite:///shop.db` | Built-in (`sqlite3`) |
| **PostgreSQL** | `postgresql://user:pass@localhost:5432/shop` | `pip install "psycopg[binary]>=3.0"` |
| **MySQL** | `mysql://user:pass@localhost:3306/shop` | `pip install "PyMySQL>=1.0"` |

### Previewing SQL Across Databases Without Connecting
Use the `sqlmigrate` command to inspect generated SQL for any database dialect:

```bash
# Preview SQLite DDL (uses PRAGMA foreign_keys and SQLite types):
PYTHONPATH=. python3 -m mini_orm.migrations sqlmigrate --dir examples/bookshop/migrations 0001 --dialect sqlite

# Preview PostgreSQL DDL (uses GENERATED IDENTITY and DOUBLE PRECISION):
PYTHONPATH=. python3 -m mini_orm.migrations sqlmigrate --dir examples/bookshop/migrations 0001 --dialect postgresql

# Preview MySQL DDL (uses backticks, AUTO_INCREMENT, and ENGINE options):
PYTHONPATH=. python3 -m mini_orm.migrations sqlmigrate --dir examples/bookshop/migrations 0001 --dialect mysql

# Preview the rollback SQL:
PYTHONPATH=. python3 -m mini_orm.migrations sqlmigrate --dir examples/bookshop/migrations 0001 --backwards --dialect sqlite
```

### Running Migrations from the Command Line
```bash
# 1. Detect changes in your models and create a new migration file:
python3 -m mini_orm.migrations makemigrations --models examples.bookshop.models --dir examples/bookshop/migrations

# 2. View current migration status on the database:
python3 -m mini_orm.migrations showmigrations --dir examples/bookshop/migrations --db shop.db

# 3. Apply all pending migrations (use --echo to inspect every executed SQL statement):
python3 -m mini_orm.migrations migrate --dir examples/bookshop/migrations --db shop.db --echo

# 4. Roll back to a previous migration or to the beginning:
python3 -m mini_orm.migrations migrate --dir examples/bookshop/migrations --db shop.db 0001
python3 -m mini_orm.migrations migrate --dir examples/bookshop/migrations --db shop.db zero
```

### Running Migrations in Python Code
You can also run migrations programmatically inside tests or application startup code:

```python
from mini_orm import Database, migrations
from myapp.models import Author, Book

db = Database("postgresql://user:pass@localhost:5432/shop", echo=True)

# Generate migration file:
migrations.makemigrations([Author, Book], "myapp/migrations")

# Apply pending migrations:
applied_messages = migrations.migrate(db, "myapp/migrations")
print(applied_messages)

# Check status:
for name, is_applied in migrations.showmigrations(db, "myapp/migrations"):
    print(f"[{'X' if is_applied else ' '}] {name}")
```

---

## Creative Examples

### Example A: Hierarchical Trees (Self-Referential Foreign Keys)
Model recursive relationships such as product categories, menus, or organizational charts:

```python
class Category(Model):
    __tablename__ = "categories"
    name = StringField(null=False)
    parent = ForeignKey("self", related_name="children", on_delete="CASCADE")

with Session(db) as session:
    root = Category(name="Electronics")
    computers = Category(name="Computers", parent=root)
    laptops = Category(name="Laptops", parent=computers)
    session.add_all([root, computers, laptops])
    session.commit()

    # Load root and prefetch children without N+1 query overhead:
    categories = session.query(Category).prefetch_related("children").all()
```

### Example B: Aggregation, Grouping & Having
Compute database-side analytics with typed aggregate expressions:

```python
from mini_orm import Avg, Count

with Session(db) as session:
    # 1. Simple Aggregates:
    stats = session.query(Book).aggregate(total=Count(), avg_price=Avg(Book.price))
    print(f"Total books: {stats['total']}, Average price: ${stats['avg_price']:.2f}")

    # 2. GROUP BY with HAVING:
    report = (
        session.query(Book)
        .group_by(Book.author)
        .having(Count() >= 2)
        .values(Book.author, book_count=Count(), mean_price=Avg(Book.price))
        .all()
    )
```

### Example C: Transaction Rollbacks & Dirty State Recovery
`mini_orm` ensures transactions remain atomic. When an operation errors or you call `session.rollback()`, tracked entities return to their pre-transaction state:

```python
with Session(db) as session:
    book = session.get(Book, 1)
    original_price = book.price
    book.price = 999.0

    # Something went wrong:
    session.rollback()

    assert book.price == original_price  # Snapshot diff resets local state!
    assert session.is_dirty(book) is False
```

---

## Intentional Omissions (Engineering Trade-Offs)

Choosing what **not** to build is essential to maintaining conceptual integrity and clarity:

| Omitted Feature | Design Rationale & Trade-Off |
| :--- | :--- |
| **No Implicit Many-to-Many Magic** | Implicit Many-to-Many models hide junction tables and cascade behaviors. Requiring explicit intermediate models makes relational flow transparent. |
| **Single-Column Primary Keys Only** | Composite primary keys complicate identity map lookups and graph sorting. Single-column keys keep identity lookups $O(1)$ and dependency graphs straightforward. |
| **No Global / Thread-Local State** | Many frameworks bind sessions to thread-local globals (`db.session`). `mini_orm` requires explicit dependency injection (`with Session(db) as session:`), eliminating hidden race conditions. |
| **No Raw String Queries in Builder** | Disallowing raw SQL fragments inside query builder methods forces all operations through the AST, preventing SQL injection vulnerabilities. |
| **Linear Migration Histories Only** | Full migration engines handle complex multi-branch merge DAGs. `mini_orm` enforces a linear migration sequence to focus on state diffing and schema alteration mechanics. |
| **No Connection Pooling** | Connection pooling introduces thread synchronization complexity. `Database` wraps direct DB-API 2.0 connections cleanly. |

---

## Quick Lab: Try It in 2 Minutes

Curious to see how it works? Follow these steps to set up and run `mini_orm` locally.

### Step 1: Create a Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Run the Test Suite (215 Tests)
```bash
pytest
```
```text
============================= test session starts ==============================
collected 215 items

tests/test_dialects.py ............                                      [  5%]
tests/test_expressions.py ........................                       [ 16%]
tests/test_fields.py ...............                                     [ 23%]
tests/test_migration_operations.py ..............................        [ 37%]
tests/test_migrations.py ..................                              [ 46%]
tests/test_models.py ....................                                [ 55%]
tests/test_query_execution.py ...............                            [ 62%]
tests/test_query_sql.py ........................                         [ 73%]
tests/test_relations.py ..................                               [ 81%]
tests/test_schema.py ..........                                          [ 86%]
tests/test_session.py .............................                      [100%]

============================= 215 passed in 0.37s ==============================
```

### Step 3: Run the Visual Tours
Watch the ORM and Migration engines run in real time with complete SQL logging:

```bash
# Core ORM tour (table creation, dirty checking, unit of work, joins, rollbacks):
PYTHONPATH=. python3 examples/tour.py

# Migration tour (autodetection, table rebuilds, column alterations, rollbacks):
PYTHONPATH=. python3 examples/migrations_tour.py
```

### Step 4: Experiment and Modify
1. Open `examples/bookshop/models.py`.
2. Add a new field: `published_year = IntegerField(default=2022)`.
3. Run `makemigrations` to watch `mini_orm` detect the new column and write `0002_*.py`:
   ```bash
   PYTHONPATH=. python3 -m mini_orm.migrations makemigrations --models examples.bookshop.models --dir examples/bookshop/migrations
   ```
4. Preview the migration DDL for SQLite, PostgreSQL, and MySQL:
   ```bash
   PYTHONPATH=. python3 -m mini_orm.migrations sqlmigrate --dir examples/bookshop/migrations 0002 --dialect postgresql
   ```

---

## Codebase Anatomy

Every module adheres to single-responsibility design:

```text
mini_orm/
├── models.py          # Metaclass, Model base class, attribute registry
├── fields.py          # Field descriptors, type coercion, validation
├── expressions.py     # Operator overloading, Expression AST nodes
├── query.py           # Immutable, chainable Query builder interface
├── compiler.py        # AST visitor compiling expressions to parameterized SQL
├── dialects.py        # Dialect definitions (SQLite, PostgreSQL, MySQL)
├── database.py        # DB-API connection handling, transaction context managers
├── schema.py          # DDL generation (CREATE TABLE, DROP TABLE)
├── graph.py           # Dependency graph and topological sorter
├── session.py         # Unit of Work, Identity Map, snapshot dirty-checking
├── relations.py       # ForeignKey descriptors, select_related, prefetch_related
└── migrations/        # Schema migration subsystem
    ├── autodetector.py # State diffing between live models and migration DAG
    ├── writer.py       # Deterministic Python migration file serializer
    ├── executor.py     # Applies and unapplies migrations in dependency order
    ├── recorder.py     # Persists applied migration state in database
    ├── schema_editor.py# Dialect DDL execution (including SQLite table rebuilds)
    └── cli.py          # Command-line interface for migrations
```

---

## Technical Takeaways

Building an ORM from first principles explores several foundational engineering concepts:

* **Python Metaprogramming**: Class-time schema inspection via metaclasses, descriptor-based attribute validation, and dunder operator overloading.
* **Database Mechanics**: Relational algebra, connection lifecycles, transaction boundaries, query planning, and dialect translation (SQLite, PostgreSQL, MySQL).
* **Architectural Patterns**: Martin Fowler's Unit of Work, Identity Map, Topological Dependency Graph Sorting, and State-Based Schema Diffing.
* **Mechanical Sympathy**: Replacing "framework magic" with clear, deterministic code backed by 215 tests and zero runtime dependencies.
