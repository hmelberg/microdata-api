"""Generisk per-bruker-dokumentlagring (askstat konto-runden fase 2):
allowlist + caps + minimal validering — klienten eier semantikken."""
import json

import pytest

import userdoc


def _doc(extra=None):
    d = {"v": 1, "updated": "2026-08-05T12:00:00.000Z"}
    if extra:
        d.update(extra)
    return json.dumps(d)


def test_gyldige_navn_og_updated_returneres():
    for name in ("askkeys", "profiles", "history"):
        assert userdoc.validate_userdoc(name, _doc()) == "2026-08-05T12:00:00.000Z"


def test_ukjent_navn_avvises():
    with pytest.raises(ValueError, match="ukjent"):
        userdoc.validate_userdoc("annet", _doc())


def test_caps_per_navn():
    for name, cap in userdoc.CAPS.items():
        stor = _doc({"x": "a" * cap})
        with pytest.raises(ValueError, match="for stort"):
            userdoc.validate_userdoc(name, stor)


def test_ikke_json_og_manglende_updated_avvises():
    with pytest.raises(ValueError, match="gyldig JSON"):
        userdoc.validate_userdoc("profiles", "{skrot")
    with pytest.raises(ValueError, match="updated"):
        userdoc.validate_userdoc("profiles", json.dumps({"v": 1}))
    with pytest.raises(ValueError, match="doc mangler"):
        userdoc.validate_userdoc("profiles", "")
    with pytest.raises(ValueError, match="JSON-objekt"):
        userdoc.validate_userdoc("profiles", "[1,2]")
