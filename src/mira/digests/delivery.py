"""Email delivery for digests, over SMTP configured from the environment.

Deliberately small: one plain-text message per digest to the recipients in
``digests.email.recipients``, sent from a worker thread so a slow mail server
cannot stall the event loop. Like the outbound webhooks, it is best effort —
it logs and returns False, it never raises.

``MIRA_SMTP_HOST``      the server; unset means no email at all
``MIRA_SMTP_PORT``      default 587 (465 means implicit TLS)
``MIRA_SMTP_USER`` / ``MIRA_SMTP_PASSWORD``  login, when both are set
``MIRA_SMTP_FROM``      the sender; defaults to the user, then ``mira@localhost``
``MIRA_SMTP_STARTTLS``  ``false`` to skip STARTTLS on a non-465 port
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20.0


def smtp_configured() -> bool:
    return bool(os.environ.get("MIRA_SMTP_HOST", "").strip())


def _header(value: str) -> str:
    """No line breaks in a header: they would start a new one."""
    return " ".join((value or "").replace("\r", " ").replace("\n", " ").split())[:200]


def build_message(subject: str, body: str, recipients: list[str]) -> EmailMessage:
    user = os.environ.get("MIRA_SMTP_USER", "").strip()
    sender = os.environ.get("MIRA_SMTP_FROM", "").strip() or user or "mira@localhost"
    message = EmailMessage()
    message["Subject"] = _header(subject)
    message["From"] = _header(sender)
    message["To"] = ", ".join(_header(r) for r in recipients)
    message.set_content(body)
    return message


def _send(message: EmailMessage) -> None:
    host = os.environ["MIRA_SMTP_HOST"].strip()
    port = int(os.environ.get("MIRA_SMTP_PORT", "587") or 587)
    user = os.environ.get("MIRA_SMTP_USER", "")
    password = os.environ.get("MIRA_SMTP_PASSWORD", "")
    starttls = os.environ.get("MIRA_SMTP_STARTTLS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    smtp: smtplib.SMTP
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT_SECONDS)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_TIMEOUT_SECONDS)
    with smtp:
        if port != 465 and starttls:
            smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(message)


async def send_email(subject: str, body: str, recipients: list[str]) -> bool:
    """Send one message. False (and a log line) on any failure or no config."""
    if not recipients or not smtp_configured():
        return False
    try:
        message = build_message(subject, body, recipients)
        await asyncio.to_thread(_send, message)
    except Exception as exc:  # noqa: BLE001 - delivery never breaks a digest
        logger.warning("Digest email to %d recipient(s) failed: %s", len(recipients), exc)
        return False
    logger.info("Digest emailed to %d recipient(s)", len(recipients))
    return True
