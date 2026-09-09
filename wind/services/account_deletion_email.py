"""
Correo de confirmación de eliminación de cuenta (nuevo flujo, 2026-09-08).

Mismo patrón que wind/services/password_reset_email.py: contexto -> render
-> tarea de Celery (nunca debe romper request_account_deletion() si el
envío falla o no se puede encolar).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string

from appConfig import EmailConfig

logger = logging.getLogger(__name__)
User = get_user_model()

_HTML_TEMPLATE = "wind/emails/account_deletion_confirmation.html"
_TEXT_TEMPLATE = "wind/emails/account_deletion_confirmation.txt"


def _resolve_display_name(*, email: str, subscriber_code: str = "") -> str:
    """Mismo orden de resolución que password_reset_email._resolve_display_name."""
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


def build_account_deletion_email_context(
    *,
    email: str,
    confirm_link: str,
    subscriber_code: str = "",
) -> dict:
    return {
        "full_name": _resolve_display_name(email=email, subscriber_code=subscriber_code),
        "confirm_link": confirm_link,
        "banner_image_url": EmailConfig.ACCOUNT_DELETION_BANNER_IMAGE_URL,
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
        "google_play_url": EmailConfig.GOOGLE_PLAY_URL,
        "app_store_url": EmailConfig.APP_STORE_URL,
    }


def render_account_deletion_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_account_deletion_confirmation_email(
    *,
    email: str,
    confirm_link: str,
    subscriber_code: str = "",
) -> None:
    """
    Renderiza y encola el correo de confirmación. No lanza -- quien llama
    (request_account_deletion) ya envuelve esto en try/except, mismo
    criterio defensivo que enqueue_password_reset_email.
    """
    if not email or not confirm_link:
        logger.warning("No se pudo encolar correo de confirmación de eliminación: falta email o enlace")
        return

    from wind.tasks import send_account_deletion_confirmation_email_task

    context = build_account_deletion_email_context(
        email=email,
        confirm_link=confirm_link,
        subscriber_code=subscriber_code,
    )
    text_body, html_body = render_account_deletion_email_bodies(context)
    send_account_deletion_confirmation_email_task.delay(
        email,
        EmailConfig.ACCOUNT_DELETION_CONFIRM_SUBJECT,
        text_body,
        html_body,
    )
    logger.info("Correo de confirmación de eliminación de cuenta encolado para %s", email)
