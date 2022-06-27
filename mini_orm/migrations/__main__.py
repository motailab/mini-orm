"""Run the migration commands: python -m mini_orm.migrations COMMAND ..."""

from .cli import main

raise SystemExit(main())
