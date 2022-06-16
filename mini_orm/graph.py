"""Sort items so that each item comes after the items that it depends on.

The ORM uses this sort (a topological sort) in two places:

* Database.create_tables(): a table with a foreign key comes after the table
  that the key refers to.
* Session.flush(): an INSERT of a book comes after the INSERT of its author,
  because the book needs the primary key of the author.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import TypeVar

from .exceptions import CircularDependencyError

T = TypeVar("T", bound=Hashable)


def sort_by_dependencies(items: Iterable[T], dependencies_of: Callable[[T], Iterable[T]]) -> list[T]:
    """Return the items in an order where each item comes after its dependencies.

    ``dependencies_of(item)`` gives the items that ``item`` depends on. The
    function ignores a dependency that is not in ``items``.

    The function keeps the original order where the dependencies allow it.
    It uses a depth-first search: before it adds an item to the result, it
    adds the dependencies of the item.
    """
    items = list(items)
    all_items = set(items)
    result: list[T] = []
    done: set[T] = set()
    # The items on the current search path. If the search finds one of them
    # again, the dependencies make a circle.
    in_progress: set[T] = set()

    def visit(item: T) -> None:
        if item in done:
            return
        if item in in_progress:
            raise CircularDependencyError(f"Circular dependency at {item!r}.")
        in_progress.add(item)
        for dependency in dependencies_of(item):
            if dependency in all_items and dependency is not item:
                visit(dependency)
        in_progress.remove(item)
        done.add(item)
        result.append(item)

    for item in items:
        visit(item)
    return result
