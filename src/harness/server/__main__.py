"""Allow ``python -m harness.server`` to run the server CLI."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
