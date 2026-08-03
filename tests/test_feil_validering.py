import json

import feil_validering


def test_gyldig_rapport_gir_ok_og_json_tekst():
    raw = json.dumps({"app": "askstat", "runs": []}).encode("utf-8")
    ok, payload, feil = feil_validering.valider_feilrapport(raw)
    assert ok and feil is None
    assert json.loads(payload)["app"] == "askstat"


def test_for_stor_kropp_avvises():
    raw = json.dumps({"app": "askstat", "x": "a" * 300_000}).encode("utf-8")
    ok, payload, feil = feil_validering.valider_feilrapport(raw)
    assert not ok and "for stor" in feil


def test_ugyldig_json_og_manglende_app_avvises():
    assert not feil_validering.valider_feilrapport(b"ikke json")[0]
    assert not feil_validering.valider_feilrapport(b'{"uten_app": 1}')[0]
    assert not feil_validering.valider_feilrapport(b'[1,2]')[0]
    assert not feil_validering.valider_feilrapport(None)[0]
