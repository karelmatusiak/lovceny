"""E-maily jsou v MVP jen zalogované. Skutečný odesílač (Resend, Brevo…) stačí
implementovat jako další třídu s metodou send() a vybrat ji přes EMAIL_BACKEND."""
import logging
import os

log = logging.getLogger("lovceny.email")


class EmailSender:
    def send(self, to: str, subject: str, body: str) -> None:
        raise NotImplementedError


class LogEmailSender(EmailSender):
    def send(self, to, subject, body):
        log.warning("[EMAIL STUB] to=%s subject=%r\n%s", to, subject, body)


def get_sender() -> EmailSender:
    backend = os.environ.get("EMAIL_BACKEND", "log")
    if backend == "log":
        return LogEmailSender()
    raise RuntimeError(f"Neznámý EMAIL_BACKEND: {backend}")
