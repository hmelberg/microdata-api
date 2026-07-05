"""Remote assembly: the shim builds datasets from the spec (trusted, outside
the facade), then safepy analyses the assembled frames. No Anvil."""
import os
import pandas as pd
from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import source_registry
import safepy_shim

PEOPLE = pd.DataFrame({"pid": list(range(60)),
                       "region": ["A" if i % 2 else "B" for i in range(60)],
                       "income": [30000 + i * 100 for i in range(60)]})


def _patch(monkeypatch, frames):
    monkeypatch.setattr(source_registry, "resolve_source",
                        lambda sid: {"source_id": sid, "kind": "url", "location": "x",
                                     "format": "csv", "level": "protected", "status": "active"})
    monkeypatch.setattr(source_registry, "load_dataframe",
                        lambda src: frames[src["source_id"]])


def test_remote_assembly_then_analysis(monkeypatch):
    _patch(monkeypatch, {"people": PEOPLE})
    spec = {"sources": ["p"], "datasets": [
        {"name": "panel", "key": "pid", "steps": [
            {"op": "import", "source": "p", "columns": ["income", "region"], "how": "left"}]}]}
    out = safepy_shim.run_extended(
        "panel.groupby('region')['income'].mean()",
        [{"alias": "p", "source_id": "people"}], dialect="pandas", assembly=spec)
    assert out["err"] is None
    assert out["results"] and "output-table" in out["results"][0]


def test_remote_assembly_missing_column_errors(monkeypatch):
    _patch(monkeypatch, {"people": PEOPLE})
    spec = {"sources": ["p"], "datasets": [
        {"name": "panel", "key": "pid", "steps": [
            {"op": "import", "source": "p", "columns": ["salary"], "how": "left"}]}]}
    out = safepy_shim.run_extended(
        "panel.sum()", [{"alias": "p", "source_id": "people"}],
        dialect="pandas", assembly=spec)
    assert out["err"] and "salary" in out["err"]


def test_remote_assembly_surfaces_row_multiplication_note(monkeypatch):
    # a many-to-many join inflates rows; the note (spec §6) must reach results
    people = pd.DataFrame({"pid": [1, 2], "income": [10, 20]})
    sales = pd.DataFrame({"pid": [1, 1, 2], "amount": [5, 6, 7]})
    _patch(monkeypatch, {"people": people, "salesrc": sales})
    spec = {"sources": ["p", "s"], "datasets": [
        {"name": "sales", "load": "s"},
        {"name": "panel", "key": "pid", "steps": [
            {"op": "import", "source": "p", "columns": ["income"], "how": "left"},
            {"op": "join", "from": "sales", "on": "pid", "how": "left"}]}]}
    out = safepy_shim.run_extended(
        "panel.groupby('pid')['amount'].sum()",
        [{"alias": "p", "source_id": "people"}, {"alias": "s", "source_id": "salesrc"}],
        dialect="pandas", assembly=spec)
    assert out["err"] is None
    assert any("rader etter join" in r for r in out["results"])


def test_remote_assembly_unknown_alias_norsk(monkeypatch):
    _patch(monkeypatch, {"people": PEOPLE})
    spec = {"sources": ["ghost"], "datasets": [
        {"name": "panel", "key": "pid", "steps": [
            {"op": "import", "source": "ghost", "columns": ["income"], "how": "left"}]}]}
    out = safepy_shim.run_extended(
        "panel.sum()", [{"alias": "p", "source_id": "people"}],
        dialect="pandas", assembly=spec)
    assert out["err"] and "ukjent kilde-alias" in out["err"]
