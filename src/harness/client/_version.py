"""Client package version. Kept separate so the CLI can report it without loading the core."""

from importlib.metadata import version

__version__ = version("harness")
