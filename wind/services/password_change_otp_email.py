"""
Correo con el código OTP de "cambiar contraseña" (Mi Cuenta, usuario ya
autenticado). Mismo patrón que wind/services/password_reset_email.py:
contexto -> render -> tarea de Celery (nunca debe romper
request_password_change_otp() si el envío falla o no se puede encolar).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string

from appConfig import EmailConfig

logger = logging.getLogger(__name__)
User = get_user_model()

_HTML_TEMPLATE = "wind/emails/password_change_otp.html"
_TEXT_TEMPLATE = "wind/emails/password_change_otp.txt"


def _resolve_display_name(*, email: str, subscriber_code: str = "") -> str:
    """Mismo criterio que wind/services/password_reset_email.py."""
    email_norm = (email or "").strip().lower()

    if email_norm:
        user = User.objects.filter(email__iexact=email_norm).first()
        if user:
            full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
            if full_name:
                return full_name

    if subscriber_code:
        from wind.models import ListOfSubscriber

        sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
        if sub:
            full_name = f"{sub.firstName or ''} {sub.lastName or ''}".strip()
            if full_name:
                return full_name

    local_part = email_norm.split("@", 1)[0].strip()
    return local_part or "Usuario"


def build_password_change_otp_email_context(
    *,
    email: str,
    code: str,
    subscriber_code: str = "",
    expiry_minutes: int = 15,
) -> dict:
    return {
        "full_name": _resolve_display_name(email=email, subscriber_code=subscriber_code),
        "code": code,
        "expiry_minutes": expiry_minutes,
        "banner_image_url": EmailConfig.PASSWORD_RESET_BANNER_IMAGE_URL,
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
        "google_play_url": EmailConfig.GOOGLE_PLAY_URL,
    }


def render_password_change_otp_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_password_change_otp_email(
    *,
    email: str,
    code: str,
    subscriber_code: str = "",
    expiry_minutes: int = 15,
) -> None:
    if not email or not code:
        logger.warning("No se pudo encolar correo de código OTP: falta email o código")
        return

    from wind.tasks import send_password_change_otp_email_task

    context = build_password_change_otp_email_context(
        email=email,
        code=code,
        subscriber_code=subscriber_code,
        expiry_minutes=expiry_minutes,
    )
    text_body, html_body = render_password_change_otp_email_bodies(context)
    send_password_change_otp_email_task.delay(
        email,
        EmailConfig.PASSWORD_CHANGE_OTP_SUBJECT,
        text_body,
        html_body,
    )
    logger.info("Correo de código OTP de cambio de contraseña encolado para %s", email)
