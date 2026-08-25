"""
Correo de notificación cuando se cambia la contraseña (cualquiera de los
dos flujos vigentes: profile/password/, o confirmación de "olvidé mi
contraseña" -- todos terminan en sync_password_locally(), que es quien
llama a enqueue_password_changed_email()).

(El tercer flujo legacy, change-password/, se dio de baja el 2026-08-25 --
ver docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md.)

Mismo patrón que wind/services/welcome_email.py: contexto -> render ->
tarea de Celery (no bloquea el cambio de contraseña si el envío falla).
"""
from __future__ import annotations

import logging

from django.template.loader import render_to_string

from appConfig import EmailConfig

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = "wind/emails/password_changed.html"
_TEXT_TEMPLATE = "wind/emails/password_changed.txt"


def _display_name(first_name: str, last_name: str, email: str) -> str:
    full_name = f"{first_name or ''} {last_name or ''}".strip()
    if full_name:
        return full_name
    local_part = (email or "").split("@", 1)[0].strip()
    return local_part or "Usuario"


def build_password_changed_email_context(
    *,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
) -> dict:
    return {
        "full_name": _display_name(first_name, last_name, email),
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
        "portal_login_url": EmailConfig.PORTAL_LOGIN_URL,
    }


def render_password_changed_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_password_changed_email(
    *,
    email: str,
    first_name: str = "",
    last_name: str = "",
) -> None:
    """
    Renderiza y encola el aviso de "contraseña actualizada". No lanza --
    quien llama (sync_password_locally) no debe fallar el cambio de
    contraseña real porque el correo de aviso no se pudo encolar/enviar.
    """
    if not email:
        logger.warning("No se pudo encolar correo de contraseña actualizada: falta email")
        return

    try:
        from wind.tasks import send_password_changed_email_task

        context = build_password_changed_email_context(
            first_name=first_name,
            last_name=last_name,
            email=email,
        )
        text_body, html_body = render_password_changed_email_bodies(context)
        send_password_changed_email_task.delay(
            email,
            EmailConfig.PASSWORD_CHANGED_SUBJECT,
            text_body,
            html_body,
        )
        logger.info("Correo de contraseña actualizada encolado para %s", email)
    except Exception:
        logger.exception("No se pudo encolar el correo de contraseña actualizada para %s", email)
