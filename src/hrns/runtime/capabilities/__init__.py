"""First-party capability plugins bundled with hrns.

These descriptors are registered by :class:`~hrns.runtime.registry.PluginRegistry`
itself, so every host sees them without installing an entry point. A bundled plugin is
available in the catalog everywhere; whether a *definition* binds and enables it stays a
host decision (see ``hrns.runtime.local``).
"""

from __future__ import annotations

from ..registry import PluginDescriptor

__all__ = ["bundled_descriptors"]


def bundled_descriptors() -> list[PluginDescriptor]:
    """Descriptors for every capability shipped inside the ``hrns`` distribution.

    Import stays lazy so ``hrns.runtime`` does not pull in a plugin's capability module
    until a binding is composed.
    """
    from .ask_user import descriptor as ask_user_descriptor
    from .coder import descriptor as coder_descriptor
    from .mcp import descriptor as mcp_descriptor
    from .skills import descriptor as skills_descriptor
    from .todo import descriptor as todo_descriptor

    return [
        ask_user_descriptor(),
        coder_descriptor(),
        mcp_descriptor(),
        skills_descriptor(),
        todo_descriptor(),
    ]
