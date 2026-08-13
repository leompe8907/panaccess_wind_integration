"""
Correo de "olvidé mi contraseña" (solicitud con enlace de restablecimiento).

Mismo patrón que wind/services/password_changed_email.py: contexto -> render
-> tarea de Celery (nunca debe romper request_password_reset() si el envío
falla o no se puede encolar).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string

from appConfig import EmailConfig

logger = logging.getLogger(__name__)
User = get_user_model()

_HTML_TEMPLATE = "wind/emails/password_reset.html"
_TEXT_TEMPLATE = "wind/emails/password_reset.txt"


def _resolve_display_name(*, email: str, subscriber_code: str = "") -> str:
    """
    Nombre a mostrar en el correo. Orden: User de Django (login portal) ->
    ListOfSubscriber (sincronizado desde PanAccess/CRM) -> parte local del
    email -> "Usuario". SubscriberEmailRegistry no guarda nombre, por eso no
    se consulta aquí.
    """
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


def build_password_reset_email_context(
    *,
    email: str,
    reset_link: str,
    subscriber_code: str = "",
) -> dict:
    return {
        "full_name": _resolve_display_name(email=email, subscriber_code=subscriber_code),
        "reset_link": reset_link,
        "banner_image_url": EmailConfig.PASSWORD_RESET_BANNER_IMAGE_URL,
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
    }


def render_password_reset_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_password_reset_email(
    *,
    email: str,
    reset_link: str,
    subscriber_code: str = "",
) -> None:
    """
    Renderiza y encola el correo de recuperación. No lanza -- quien llama
    (request_password_reset) ya envuelve esto en try/except, pero se
    mantiene el mismo criterio defensivo que enqueue_password_changed_email.
    """
    if not email or not reset_link:
        logger.warning("No se pudo encolar correo de recuperación: falta email o enlace")
        return

    from wind.tasks import send_password_reset_email_task

    context = build_password_reset_email_context(
        email=email,
        reset_link=reset_link,
        subscriber_code=subscriber_code,
    )
    text_body, html_body = render_password_reset_email_bodies(context)
    send_password_reset_email_task.delay(
        email,
        EmailConfig.PASSWORD_RESET_SUBJECT,
        text_body,
        html_body,
    )
    logger.info("Correo de recuperación de contraseña encolado para %s", email)
