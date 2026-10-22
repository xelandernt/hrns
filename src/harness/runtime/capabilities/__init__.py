"""First-party capability plugins bundled with Harness.

These descriptors are registered by :class:`~harness.runtime.registry.PluginRegistry`
itself, so every host sees them without installing an entry point. A bundled plugin is
available in the catalog everywhere; whether a *definition* binds and enables it stays a
host decision (see ``harness.runtime.local``).
"""

from __future__ import annotations

from ..registry import PluginDescriptor

__all__ = ["bundled_descriptors"]


def bundled_descriptors() -> list[PluginDescriptor]:
    """Descriptors for every capability shipped inside the ``harness`` distribution.

    Import stays lazy so ``harness.runtime`` does not pull in a plugin's capability module
    until a binding is composed.
    """
    from .coder import descriptor as coder_descriptor
    from .skills import descriptor as skills_descriptor

    return [coder_descriptor(), skills_descriptor()]
