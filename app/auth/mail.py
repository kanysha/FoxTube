"""Bounded SMTP delivery; credentials and recovery links never enter logs."""

import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import Settings


def configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_recovery(settings: Settings, email: str, token: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise ValueError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = "Восстановление пароля FoxTube"
    message["From"] = str(settings.smtp_from)
    message["To"] = email
    link = f"{str(settings.public_origin).rstrip('/')}/password/reset#token={token}"
    message.set_content(
        "Вы запросили восстановление пароля FoxTube.\n\n"
        f"Откройте ссылку в течение 30 минут:\n{link}\n\n"
        "Если вы не запрашивали восстановление, проигнорируйте письмо.\n"
        "Не пересылайте эту ссылку другим людям."
    )
    context = ssl.create_default_context()
    if settings.smtp_security == "tls":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout,
            context=context,
        )
    else:
        smtp = smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
        )
    try:
        with smtp:
            if settings.smtp_security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if settings.smtp_username:
                smtp.login(
                    settings.smtp_username,
                    settings.smtp_password.get_secret_value()
                    if settings.smtp_password
                    else "",
                )
            smtp.send_message(message)
    finally:
        smtp.close()
