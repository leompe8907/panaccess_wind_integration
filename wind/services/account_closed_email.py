"""
Correo de notificación cuando se cierra/elimina la cuenta de un suscriptor
-- tanto por el cierre manual/API (`close_subscriber_account`,
wind/services/subscriber_closure.py) como por el borrado automático que
corre la sincronización periódica cuando un suscriptor ya no existe en
PanAccess (`_delete_local_subscribers_not_in_remote`,
wind/functions/getSubscriber.py). Ver hallazgo Alto #4,
docs/AUDITORIA_CONSOLIDADA_2026-08-24.md, y
docs/CIERRE_CUENTA_REVOCACION_SYNC_2026-08-26.md.

Mismo patrón que wind/services/password_changed_email.py: contexto ->
render -> tarea de Celery (no bloquea el cierre de cuenta si el envío
falla).
"""
from __future__ import annotations

import logging

from django.template.loader import render_to_string

from appConfig import EmailConfig

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = "wind/emails/account_closed.html"
_TEXT_TEMPLATE = "wind/emails/account_closed.txt"


def _display_name(first_name: str, last_name: str, email: str) -> str:
    full_name = f"{first_name or ''} {last_name or ''}".strip()
    if full_name:
        return full_name
    local_part = (email or "").split("@", 1)[0].strip()
    return local_part or "Usuario"


def build_account_closed_email_context(
    *,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
) -> dict:
    return {
        "full_name": _display_name(first_name, last_name, email),
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
    }


def render_account_closed_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_account_closed_email(
    *,
    email: str,
    first_name: str = "",
    last_name: str = "",
) -> None:
    """
    Renderiza y encola el aviso de "cuenta cerrada". No lanza -- quien
    llama (close_subscriber_account / _delete_local_subscribers_not_in_remote)
    no debe fallar el cierre real porque el correo de aviso no se pudo
    encolar/enviar. No hace nada (con warning en el log) si no hay un email
    real disponible -- nunca manda a un dominio sintético/de relleno.
    """
    email = (email or "").strip().lower()
    if not email or email.endswith("@subscribers.wind.local"):
        logger.warning(
            "No se pudo encolar correo de cuenta cerrada: sin email real disponible (%r)",
            email,
        )
        return

    try:
        from wind.tasks import send_account_closed_email_task

        context = build_account_closed_email_context(
            first_name=first_name,
            last_name=last_name,
            email=email,
        )
        text_body, html_body = render_account_closed_email_bodies(context)
        send_account_closed_email_task.delay(
            email,
            EmailConfig.ACCOUNT_CLOSED_SUBJECT,
            text_body,
            html_body,
        )
        logger.info("Correo de cuenta cerrada encolado para %s", email)
    except Exception:
        logger.exception("No se pudo encolar el correo de cuenta cerrada para %s", email)
