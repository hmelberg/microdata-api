"""POST /feil — mottak av feilrapporter fra askstat (spec i askstat-repoet:
docs/superpowers/specs/2026-08-03-datasok-og-nedlasting-v1-design.md §1a).

Bevisst dumt og NØKKELFRITT (avsenderen er en nettleser uten hemmeligheter):
størrelsesvakt + JSON-parse + lagre rått. Aldri mer logikk her — endepunktet
skal aldri trenge en ny Anvil-synk. Analyse: eksporter tabellen, jobb offline.
"""
import datetime

import anvil.server
from anvil.server import HttpResponse
from anvil.tables import app_tables

import feil_validering


@anvil.server.http_endpoint("/feil", methods=["POST"], cross_site_session=False, enable_cors=True)
def http_feil():
    req = anvil.server.request
    raw = req.body.get_bytes() if req.body else b""
    ok, payload, feil = feil_validering.valider_feilrapport(raw)
    if not ok:
        return HttpResponse(400, feil)
    app_tables.feilrapporter.add_row(
        mottatt=datetime.datetime.now(datetime.timezone.utc), payload=payload)
    return HttpResponse(204, "")
