"""Silence the expected experimental warning for the pinned upstream ACP adapter."""

from __future__ import annotations

import warnings

from pydantic_ai_harness.experimental import HarnessExperimentalWarning

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)
