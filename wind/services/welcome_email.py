"""
Correo de bienvenida con credenciales post-registro.
"""
from __future__ import annotations

import logging

from django.template.loader import render_to_string

from appConfig import EmailConfig
from wind.functions.getSubscriberLoginInfo import CallGetSubscriberLoginInfo

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = "wind/emails/welcome_credentials.html"
_TEXT_TEMPLATE = "wind/emails/welcome_credentials.txt"


def _display_name(first_name: str, last_name: str, email: str) -> str:
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    local_part = (email or "").split("@", 1)[0].strip()
    return local_part or "Usuario"


def resolve_subscriber_login_email(
    *,
    subscriber_code: str,
    email_hint: str = "",
) -> str:
    """Email del suscriptor para mostrar como usuario (correo, pantalla credenciales)."""
    email = (email_hint or "").strip().lower()
    if email:
        return email

    from wind.models import ListOfSubscriber, SubscriberEmailRegistry

    reg = SubscriberEmailRegistry.objects.filter(subscriber_code=subscriber_code).first()
    if reg and reg.email:
        return reg.email.strip().lower()

    sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    if sub and sub.emails:
        return sub.emails.strip().lower()

    return ""


def _resolve_credentials(
    *,
    email: str,
    subscriber_code: str,
    is_social_account: bool,
) -> tuple[str, str]:
    """Devuelve (usuario, contraseña) para mostrar en el correo."""
    username = (email or "").strip().lower()
    if is_social_account:
        return username, EmailConfig.SOCIAL_PASSWORD_MESSAGE

    try:
        login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)
    except Exception as exc:
        logger.warning(
            "No se pudieron obtener credenciales PanAccess para %s (%s): %s",
            username,
            subscriber_code,
            exc,
        )
        return username, "No pudimos cargar tu contraseña. Revisa el portal WindTV o contacta soporte."

    password = (login_info.get("password") or "").strip()
    if not password:
        password = "No pudimos cargar tu contraseña. Revisa el portal WindTV o contacta soporte."
    return username, password


def build_welcome_email_context(
    *,
    first_name: str,
    last_name: str,
    email: str,
    subscriber_code: str,
    is_social_account: bool = False,
) -> dict:
    username, password_display = _resolve_credentials(
        email=email,
        subscriber_code=subscriber_code,
        is_social_account=is_social_account,
    )
    return {
        "full_name": _display_name(first_name, last_name, email),
        "username": username,
        "password_display": password_display,
        "is_social_account": is_social_account,
        "support_email": EmailConfig.SUPPORT_ADDRESS,
        "support_phone": EmailConfig.SUPPORT_PHONE,
        "terms_url": EmailConfig.TERMS_URL,
        "google_play_url": EmailConfig.GOOGLE_PLAY_URL,
        "app_store_url": EmailConfig.APP_STORE_URL,
    }


def render_welcome_email_bodies(context: dict) -> tuple[str, str]:
    text_body = render_to_string(_TEXT_TEMPLATE, context).strip()
    html_body = render_to_string(_HTML_TEMPLATE, context).strip()
    return text_body, html_body


def enqueue_welcome_credentials_email(
    *,
    first_name: str,
    last_name: str,
    email: str,
    subscriber_code: str,
    is_social_account: bool = False,
) -> None:
    """Renderiza y encola el correo de bienvenida (no bloquea el registro si falla)."""
    from wind.tasks import send_welcome_credentials_email_task

    context = build_welcome_email_context(
        first_name=first_name,
        last_name=last_name,
        email=email,
        subscriber_code=subscriber_code,
        is_social_account=is_social_account,
    )
    text_body, html_body = render_welcome_email_bodies(context)
    send_welcome_credentials_email_task.delay(
        email,
        EmailConfig.WELCOME_SUBJECT,
        text_body,
        html_body,
    )
    logger.info("Correo de bienvenida encolado para %s (suscriptor %s)", email, subscriber_code)
