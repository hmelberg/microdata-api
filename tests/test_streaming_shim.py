"""Offline tests for progressive release: safepy_shim.run_extended(on_progress=...)
streams one client-shaped fragment per released statement. No Anvil, no network —
the registry is monkeypatched (same pattern as test_he_sources)."""

import numpy as np
import pandas as pd

import safepy_shim
import source_registry


def _frame():
    n = 200
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "sex": rng.choice(["F", "M"], n),
        "region": rng.choice(["A", "B"], n),
        "salary": rng.integers(30000, 90000, n).astype(float),
    })


def _patch_registry(monkeypatch):
    src = {"source_id": "s", "level": "protected", "format": "", "status": "active"}
    monkeypatch.setattr(source_registry, "resolve_source",
                        lambda sid: dict(src, source_id=sid))
    monkeypatch.setattr(source_registry, "load_dataframe", lambda s: _frame())


def test_on_progress_streams_fragment_per_released_statement(monkeypatch):
    _patch_registry(monkeypatch)
    got = []
    out = safepy_shim.run_extended(
        "df.groupby('sex')['salary'].mean()\ndf['salary'].mean()",
        [{"alias": "df", "source_id": "s"}], dialect="pandas",
        on_progress=got.append)
    assert out["err"] is None and len(out["results"]) == 2
    assert [f["kind"] for f in got] == ["html", "html"]
    # streamed fragments are byte-identical to the final results list
    assert [f["html"] for f in got] == out["results"]


def test_on_progress_absent_changes_nothing(monkeypatch):
    _patch_registry(monkeypatch)
    out = safepy_shim.run_extended(
        "df['salary'].mean()", [{"alias": "df", "source_id": "s"}], dialect="pandas")
    assert out["err"] is None and len(out["results"]) == 1


def test_leaf_fragment_defers_charts_to_final_render():
    frag = safepy_shim._leaf_fragment(
        {"kind": "chart", "payload": {"format": "plotly", "content": "{}"}})
    assert frag["kind"] == "note" and "figur" in frag["html"]
