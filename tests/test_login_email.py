"""Login-eposten: per-app-branding (askstat konto-runden 2026-08-05) —
askstat-brukere skal ALDRI se Microdata-branding eller micro.fhi.dev-lenken."""
import login_email


def test_askstat_branding_og_lenke():
    subject, html = login_email.build_login_email(
        "brave-otter-kicked-golden-drum", lang="en", app="askstat")
    assert "AskStat" in subject
    assert "https://ask.melberg.app/?login=brave-otter-kicked-golden-drum" in html
    assert "Microdata" not in html
    assert "micro.fhi.dev" not in html
    assert "brave otter kicked golden drum" in html   # setningsform i boksen


def test_default_og_ukjent_app_gir_microdata():
    for app in ("", "microdata", "ukjent-app"):
        subject, html = login_email.build_login_email("a-b-c", lang="no", app=app or "microdata")
        assert "Microdata Script Runner" in subject
        assert "https://micro.fhi.dev/?login=a-b-c" in html


def test_ukjent_app_faller_tilbake_uten_injeksjon():
    subject, html = login_email.build_login_email(
        "a-b-c", lang="en", app="<script>x</script>")
    assert "Microdata Script Runner" in subject
    assert "<script>" not in html
