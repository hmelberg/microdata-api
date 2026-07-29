"""Federert pass-through (fase 2, safestat spec 2026-07-29 §5-6): m2py_shim
må sende federated/fed_round videre til den syncede kjernen, ellers virker
node-federering bare mot safestat-node — ikke mot denne serveren. No Anvil."""
import os
import pandas as pd
from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import source_registry
import m2py_shim

PEOPLE = pd.DataFrame({"grp": [1] * 6 + [2] * 7,
                       "y": [0, 1] * 6 + [1],
                       "x": [float(i) for i in range(13)]})


def _patch(monkeypatch):
    monkeypatch.setattr(source_registry, "resolve_source",
                        lambda sid: {"source_id": sid, "kind": "url", "location": "x",
                                     "format": "csv", "level": "public", "status": "active"})
    monkeypatch.setattr(source_registry, "load_dataframe", lambda src: PEOPLE)


def test_federated_returns_stats(monkeypatch):
    _patch(monkeypatch)
    out = m2py_shim.run_extended(
        "create-dataset demo\ntabulate grp",
        [{"alias": "demo", "source_id": "people"}], federated=True)
    assert out["err"] is None
    assert out["stats"][0]["kind"] == "tabulate"
    assert {r["grp"]: r["n"] for r in out["stats"][0]["records"]} == {1: 6, 2: 7}


def test_fed_round_returns_logit_gradient(monkeypatch):
    _patch(monkeypatch)
    out = m2py_shim.run_extended(
        "create-dataset demo\nlogit y x",
        [{"alias": "demo", "source_id": "people"}],
        federated=True, fed_round={"beta": [0.0, 0.0]})
    assert out["err"] is None
    assert out["stats"][0]["kind"] == "logit_round"
    assert "grad" in out["stats"][0] and "hess" in out["stats"][0]


def test_not_federated_has_no_stats(monkeypatch):
    _patch(monkeypatch)
    out = m2py_shim.run_extended(
        "create-dataset demo\ntabulate grp",
        [{"alias": "demo", "source_id": "people"}])
    assert "stats" not in out
