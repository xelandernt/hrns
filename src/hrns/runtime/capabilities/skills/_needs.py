"""Resources the Agent Skills capability cannot work without."""

from __future__ import annotations

from ...resources import WORKSPACE, ResourceSlot

NEEDS: tuple[ResourceSlot[object], ...] = (WORKSPACE,)
