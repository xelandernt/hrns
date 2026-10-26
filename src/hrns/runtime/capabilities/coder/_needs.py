"""Resources the coder capability cannot work without.

Kept in a lightweight module so descriptor discovery can read the contract without importing
the tool implementations.
"""

from __future__ import annotations

from ...resources import WORKSPACE, ResourceSlot

NEEDS: tuple[ResourceSlot[object], ...] = (WORKSPACE,)
