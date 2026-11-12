"""Сповіщення поштою через SMTP. Без SMTP_HOST і SMTP_FROM канал email вимкнений."""

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings

SMTP_TIMEOUT_SEC = 15
SSL_PORT = 465


class EmailError(Exception):
    """Лист не надіслано."""


def email_enabled() -> bool:
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from)


def _send(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    smtp_class = smtplib.SMTP_SSL if settings.smtp_port == SSL_PORT else smtplib.SMTP
    try:
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SEC) as smtp:
            if smtp_class is smtplib.SMTP:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(type(exc).__name__) from None


async def send_email(to: str, subject: str, body: str) -> None:
    if not email_enabled():
        raise EmailError("SMTP не налаштований")
    await asyncio.to_thread(_send, to, subject, body)
