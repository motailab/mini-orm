"""The migration writer: it writes the Python source of a migration file."""

from __future__ import annotations

import dataclasses
import datetime
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..exceptions import MigrationError
from .operations import Operation

TEMPLATE = '''"""Migration {name}.

mini_orm made this file with makemigrations. You can change it.{notes}
"""

{imports}from mini_orm import migrations


class Migration(migrations.Migration):
    dependencies = {dependencies}
    operations = {operations}
'''


def migration_source(
    name: str,
    dependencies: Sequence[str],
    operations: Sequence[Operation],
    notes: Sequence[str] = (),
) -> str:
    """Return the Python source of a migration file."""
    notes_text = "".join("\n\n" + textwrap.fill(f"NOTE: {note}", width=76) for note in notes)
    # repr() of a date default is "datetime.date(...)", so the file needs the module.
    uses_dates = any(
        isinstance(getattr(operation, "default", None), datetime.date) for operation in operations
    )
    return TEMPLATE.format(
        name=name,
        notes=notes_text,
        imports="import datetime\n\n" if uses_dates else "",
        dependencies=render(list(dependencies), "    "),
        operations=render(list(operations), "    "),
    )


def write_migration(
    directory: str | Path,
    name: str,
    dependencies: Sequence[str],
    operations: Sequence[Operation],
    notes: Sequence[str] = (),
) -> Path:
    """Write a migration file into the directory. Return the path of the file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    if path.exists():
        raise MigrationError(f"The file {path} exists already.")
    path.write_text(migration_source(name, dependencies, operations, notes))
    return path


def render(value: Any, indent: str) -> str:
    """Return the Python source for a value: an operation, a column, a list or a constant.

    ``indent`` is the indentation of the line where the value starts.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _render_dataclass(value, indent)
    if isinstance(value, list):
        if not value:
            return "[]"
        inner = indent + "    "
        items = "".join(f"{inner}{render(item, inner)},\n" for item in value)
        return f"[\n{items}{indent}]"
    # A string, a number, a bool, None or a tuple: repr() gives valid Python.
    return repr(value)


def _render_dataclass(obj: Any, indent: str) -> str:
    """Write a call of the class: required fields as positional arguments,
    other fields as keyword arguments, but only if they differ from the default.

    The migrations package exports each class (an operation or Column), so
    the call uses the prefix "migrations.".
    """
    arguments = []
    for field in dataclasses.fields(obj):
        value = getattr(obj, field.name)
        if field.default is dataclasses.MISSING:
            arguments.append(render(value, indent))
        elif value != field.default:
            arguments.append(f"{field.name}={render(value, indent)}")
    return f"migrations.{type(obj).__name__}({', '.join(arguments)})"
