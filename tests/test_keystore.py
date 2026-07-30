"""Ren validering for kontosynk av nøkkellageret (safestat key store fase 2)."""
import json

import pytest

import keystore


def _doc(**kw):
    base = {"v": 2, "entries": {"fred": {"policy": "open", "value": "x"}},
            "updated": "2026-07-30T10:00:00.000Z"}
    base.update(kw)
    return json.dumps(base)


def test_valid_doc_returns_updated():
    assert keystore.validate_doc(_doc()) == "2026-07-30T10:00:00.000Z"


def test_rejects_non_json():
    with pytest.raises(ValueError, match="gyldig JSON"):
        keystore.validate_doc("{ikke json")


def test_rejects_wrong_version_and_shape():
    with pytest.raises(ValueError, match="v2"):
        keystore.validate_doc(json.dumps({"v": 1, "entries": {}, "updated": "x"}))
    with pytest.raises(ValueError, match="v2"):
        keystore.validate_doc(json.dumps(["liste"]))


def test_rejects_missing_entries_or_updated():
    with pytest.raises(ValueError, match="entries"):
        keystore.validate_doc(json.dumps({"v": 2, "updated": "x"}))
    with pytest.raises(ValueError, match="updated"):
        keystore.validate_doc(json.dumps({"v": 2, "entries": {}}))


def test_rejects_oversized_doc():
    big = _doc(entries={"a": {"policy": "open", "value": "x" * 70000}})
    with pytest.raises(ValueError, match="for stort"):
        keystore.validate_doc(big)


def test_rejects_empty():
    with pytest.raises(ValueError, match="mangler"):
        keystore.validate_doc("")
