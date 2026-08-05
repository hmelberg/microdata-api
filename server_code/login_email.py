"""Login-epostens innhold (ren modul — pytest uten anvil). Flyttet fra
auth.py 2026-08-05 (askstat konto-runden: per-app-branding + setningskoder)."""

# Per-app-branding i login-eposten (askstat konto-runden 2026-08-05): klienten
# sender app-feltet; ukjent/manglende app → dagens microdata-tekst (safestat
# oppdaterer klienten sin når den vil). Allowlist — aldri fritekst inn i eposten.
LOGIN_APPS = {
    "microdata": {"name": "Microdata Script Runner", "url": "https://micro.fhi.dev/"},
    "askstat": {"name": "AskStat", "url": "https://ask.melberg.app/"},
}


def build_login_email(code: str, *, lang: str = "no", app: str = "microdata") -> tuple:
    """Bygg (subject, html) for login-eposten — ren funksjon (pytest).
    Koden vises med MELLOMROM (setningsform, lettere å lese/huske); lenken
    bruker den kanoniske bindestrek-formen (URL-trygg). Normalisereren gjør
    formene likeverdige ved innlogging."""
    meta = LOGIN_APPS.get(app) or LOGIN_APPS["microdata"]
    url = meta["url"] + "?login=" + code
    pretty = code.replace("-", " ")
    box = ("<p style=\"font-size: 18px; font-family: monospace; padding: 12px; "
           f"background: #f4f4f4; border-radius: 4px;\"><strong>{pretty}</strong></p>")
    if lang == "en":
        subject = f"Sign in to {meta['name']}"
        html = (
            "<p>Hi,</p>"
            "<p>Your sign-in code:</p>" + box +
            f"<p>Paste it in the login dialog in {meta['name']}. "
            "The code is valid for 30 days and works on any device — use the "
            "same code on your other machines, and any synced keys unlock "
            "automatically.</p>"
            f"<p>Or click here to sign in directly on this device: "
            f"<a href=\"{url}\">Sign in</a></p>"
            "<p>If you did not request this, you can ignore this email.</p>"
        )
    else:
        subject = f"Logg inn til {meta['name']}"
        html = (
            "<p>Hei,</p>"
            "<p>Din pålogginskode:</p>" + box +
            f"<p>Lim den inn i pålogginsdialogen i {meta['name']}. "
            "Koden er gyldig i 30 dager og fungerer på hvilken som helst "
            "enhet — bruk samme kode på de andre maskinene dine, så låses "
            "eventuelle synkede nøkler opp automatisk.</p>"
            f"<p>Eller klikk her for å logge inn direkte på denne enheten: "
            f"<a href=\"{url}\">Logg inn</a></p>"
            "<p>Hvis du ikke ba om dette, kan du ignorere denne e-posten.</p>"
        )
    return subject, html
