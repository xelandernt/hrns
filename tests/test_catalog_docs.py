"""The published coverage matrix must match the catalog data.

`docs/CATALOG.md` is the user-facing honesty contract.  This test prevents drift between the
documented inventory (plugin id, real import target, tier, lifecycle) and the executable
catalog, and checks the published totals.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_runtime import catalog
from harness_runtime.catalog import CatalogKind, CatalogTier

DOC = Path(__file__).resolve().parents[1] / "docs" / "CATALOG.md"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_every_catalog_plugin_appears_with_its_real_import_and_tier() -> None:
    text = _doc_text()
    missing: list[str] = []
    mismatched: list[str] = []
    for entry in catalog.iter_catalog():
        if entry.plugin_id not in text:
            missing.append(entry.plugin_id)
            continue
        rows = [
            line
            for line in text.splitlines()
            if f"`{entry.plugin_id}`" in line and line.lstrip().startswith("|")
        ]
        assert rows, f"{entry.plugin_id} has no table row"
        row = rows[0]
        if entry.upstream_import is not None:
            if f"`{entry.upstream_import}`" not in row:
                mismatched.append(f"{entry.plugin_id}: import {entry.upstream_import}")
        if entry.kind is CatalogKind.CAPABILITY and entry.tier is not CatalogTier.UNAVAILABLE:
            if entry.tier.value not in row:
                mismatched.append(f"{entry.plugin_id}: tier {entry.tier.value}")
            lifecycle = f"{entry.change_mode}/{entry.state_mode}"
            if lifecycle not in row:
                mismatched.append(f"{entry.plugin_id}: lifecycle {lifecycle}")
        if entry.tier is CatalogTier.UNAVAILABLE and entry.tier.value not in row:
            mismatched.append(f"{entry.plugin_id}: tier {entry.tier.value}")
        if entry.managed and entry.config_model is not None:
            if f"`{entry.config_model.__name__}`" not in row:
                mismatched.append(f"{entry.plugin_id}: config {entry.config_model.__name__}")
    assert not missing, f"documented inventory misses: {missing}"
    assert not mismatched, "coverage matrix drift:\n" + "\n".join(mismatched)


def test_published_totals_match_the_catalog() -> None:
    text = _doc_text()
    tiers = catalog.describe_tiers()
    total = sum(len(ids) for ids in tiers.values())
    managed = sum(entry.managed for entry in catalog.iter_catalog())
    expected = (
        f"| **Total** | **{total}** | **{len(tiers['verified'])}** | "
        f"**{len(tiers['install_required'])}** | **{len(tiers['unavailable'])}** | "
        f"**{managed}** |"
    )
    assert expected in text, f"missing or stale totals row; expected {expected!r}"
    assert "92" in text and "reviewed capability plugins are registered" in text


def test_unavailable_entries_are_documented_as_unavailable() -> None:
    text = _doc_text()
    for entry in catalog.unavailable_entries():
        row = next(line for line in text.splitlines() if f"`{entry.plugin_id}`" in line)
        assert "unavailable" in row.lower()
        assert entry.upstream_import is None


def test_managed_and_unmanaged_entries_are_distinguished() -> None:
    text = _doc_text()
    for entry in catalog.iter_catalog():
        if entry.kind is CatalogKind.RESOURCE:
            continue
        if not entry.managed:
            row = next(line for line in text.splitlines() if f"`{entry.plugin_id}`" in line)
            assert "documented only" in row or entry.tier is CatalogTier.UNAVAILABLE, (
                f"{entry.plugin_id} is unmanaged but not documented as a gap"
            )


def test_documented_verification_counts_match_tests() -> None:
    text = _doc_text()
    assert re.search(r"`tests/test_catalog_factories\.py` \(17 tests\)", text)
    assert re.search(r"`tests/test_catalog_starters_examples\.py` \(19 tests\)", text)
    assert "62" in text and "runtime capabilities" in text
