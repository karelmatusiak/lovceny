"""Odesílání e-mailů.

EMAIL_BACKEND=log (výchozí) – e-maily jen do logu (vývoj, testy, chybějící klíč).
EMAIL_BACKEND=brevo – transakční API Brevo (https://api.brevo.com/v3/smtp/email), free tier 300 e-mailů/den.
  BREVO_API_KEY, EMAIL_FROM (ověřený odesílatel v Brevu), EMAIL_FROM_NAME (výchozí „Lovceny“).
Když Brevo selže nebo chybí klíč, e-mail se zaloguje a send() vrátí False (volající pak třeba nezapíše
„odesláno“ a zkusí to příště).
"""
import logging
import os

import httpx

log = logging.getLogger("lovceny.email")
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


class EmailSender:
    name = "base"

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> bool:
        raise NotImplementedError


class LogEmailSender(EmailSender):
    name = "log"

    def send(self, to, subject, text, html=None):
        log.warning("[EMAIL STUB] to=%s subject=%r\n%s", to, subject, text)
        return True


class BrevoEmailSender(EmailSender):
    name = "brevo"

    def __init__(self, api_key: str, sender_email: str, sender_name: str = "Lovceny", timeout: float = 10):
        self.api_key, self.sender_email, self.sender_name, self.timeout = api_key, sender_email, sender_name, timeout

    def send(self, to, subject, text, html=None):
        body = {"sender": {"email": self.sender_email, "name": self.sender_name},
                "to": [{"email": to}], "subject": subject, "textContent": text, "tags": ["lovceny"]}
        if html:
            body["htmlContent"] = html
        try:
            r = httpx.post(BREVO_URL, json=body, timeout=self.timeout,
                           headers={"api-key": self.api_key, "accept": "application/json"})
        except httpx.HTTPError as e:
            log.error("Brevo: síťová chyba %s – e-mail pro %s neodeslán (%r)", type(e).__name__, _mask(to), subject)
            return False
        if r.status_code in (200, 201, 202):
            log.info("Brevo: odesláno %s (%r)", _mask(to), subject)
            return True
        log.error("Brevo: HTTP %s %s – e-mail pro %s neodeslán (%r)", r.status_code, r.text[:200], _mask(to), subject)
        return False


def _mask(email: str) -> str:
    user, _, dom = email.partition("@")
    return (user[:2] + "***@" + dom) if dom else "***"


def get_sender() -> EmailSender:
    backend = os.environ.get("EMAIL_BACKEND", "log").strip().lower()
    if backend == "brevo":
        key = os.environ.get("BREVO_API_KEY", "").strip()
        sender = os.environ.get("EMAIL_FROM", "").strip()
        if key and sender:
            return BrevoEmailSender(key, sender, os.environ.get("EMAIL_FROM_NAME", "Lovceny").strip() or "Lovceny")
        log.error("EMAIL_BACKEND=brevo, ale chybí BREVO_API_KEY nebo EMAIL_FROM – e-maily jdou jen do logu")
        return LogEmailSender()
    if backend != "log":
        log.error("Neznámý EMAIL_BACKEND=%r – e-maily jdou jen do logu", backend)
    return LogEmailSender()
