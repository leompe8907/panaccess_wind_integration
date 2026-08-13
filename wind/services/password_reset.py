"""
Recuperación de contraseña PanAccess vía email.

Flujo:
  1. request_password_reset(email) → token firmado + email (si el correo existe)
  2. confirm_password_reset(token, new_pass) → PanAccess + sync local
"""
from __future__ import annotations

import base64
import hashlib
import logging

from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

from wind.exceptions import (
    PanAccessException,
    PanAccessAPIError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAuthenticationError,
    PanAccessSessionError,
    PanAccessRateLimitError,
)
from wind.models import PasswordResetTokenUse, SubscriberEmailRegistry, SubscriberLoginInfo
from wind.services import get_panaccess
from wind.services.device_session_service import revoke_all_device_sessions_for_subscriber
from wind.services.jwt_invalidation import mark_password_changed

logger = logging.getLogger(__name__)
User = get_user_model()

PASSWORD_RESET_SALT = "wind.password-reset"
PASSWORD_RESET_MAX_AGE_SECONDS = 60 * 60

GENERIC_FORGOT_MESSAGE = (
    "Si el correo está registrado en Wind, recibirás un enlace para restablecer "
    "tu contraseña. Revisa también la carpeta de spam. El enlace expira en 60 minutos."
)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def build_reset_token(subscriber_code: str, email: str) -> str:
    signer = TimestampSigner(salt=PASSWORD_RESET_SALT)
    email_b64 = base64.urlsafe_b64encode(normalize_email(email).encode("utf-8")).decode("ascii")
    payload = f"{subscriber_code}|{email_b64}"
    return signer.sign(payload)


def parse_reset_token(token: str) -> tuple[str, str]:
    """Devuelve (subscriber_code, email). Lanza BadSignature o SignatureExpired."""
    signer = TimestampSigner(salt=PASSWORD_RESET_SALT)
    raw = signer.unsign(token, max_age=PASSWORD_RESET_MAX_AGE_SECONDS)
    parts = str(raw).split("|", 1)
    if len(parts) != 2:
        raise BadSignature("Formato de token inválido")
    subscriber_code, email_b64 = parts
    try:
        email = base64.urlsafe_b64decode(email_b64.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise BadSignature("Email en token inválido") from exc
    return subscriber_code, normalize_email(email)


def _token_used_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"password_reset:used:{digest}"


def is_reset_token_used(token: str) -> bool:
    """
    La BD es la fuente de verdad (no falla "abierto" si Redis está caído --
    antes, is_reset_token_used dependía solo de Redis y una caída dejaba
    reutilizable cualquier token filtrado durante toda la ventana de la
    caída). Redis se usa además como caché rápida best-effort.
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if PasswordResetTokenUse.objects.filter(token_hash=digest).exists():
        return True

    try:
        from appConfig import RedisConfig

        return RedisConfig.get_client().get(_token_used_key(token)) is not None
    except Exception:
        logger.warning("No se pudo verificar token de reset en Redis (se usó solo la BD)", exc_info=True)
        return False


def mark_reset_token_used(token: str) -> None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        PasswordResetTokenUse.objects.get_or_create(token_hash=digest)
    except Exception:
        logger.warning("No se pudo marcar token de reset como usado en BD", exc_info=True)

    try:
        from appConfig import RedisConfig

        RedisConfig.get_client().set(
            _token_used_key(token),
            b"1",
            ex=PASSWORD_RESET_MAX_AGE_SECONDS,
        )
    except Exception:
        logger.warning("No se pudo marcar token de reset como usado en Redis (ya quedó en BD)", exc_info=True)


def reset_password_in_panaccess(subscriber_code: str, new_pass: str) -> None:
    panaccess = get_panaccess()
    response = panaccess.call(
        "resetSubscriberPassword",
        {"code": subscriber_code, "newPass": new_pass, "hash": False},
    )
    if not response.get("success"):
        raise PanAccessException(
            response.get("errorMessage", "Error al restablecer contraseña en PanAccess")
        )


def sync_password_locally(subscriber_code: str, email: str, new_pass: str) -> None:
    """Actualiza caché local y User Django tras un cambio de contraseña."""
    login_info = SubscriberLoginInfo.objects.filter(subscriberCode=subscriber_code).first()
    if login_info:
        login_info.set_password(new_pass)
        login_info.save(update_fields=["password_hash"])

    user = User.objects.filter(email__iexact=normalize_email(email)).first()
    if user:
        user.set_password(new_pass)
        user.save(update_fields=["password"])
        # Invalida sesiones JWT previas (access + refresh) -- ver
        # wind/services/jwt_invalidation.py. Cubre tanto "olvidé mi
        # contraseña" como "cambiar contraseña" desde el perfil, porque
        # ambos flujos llaman a esta misma función.
        mark_password_changed(user)

    # Fase 4: además de cortar el JWT, revoca en bloque cualquier
    # dispositivo vinculado (`DeviceSession`, Fase 3) de este suscriptor --
    # sin esto, una TV/app ya vinculada seguía "activa" en el dashboard y
    # no se enteraba del cambio de contraseña hasta reconectarse por su
    # cuenta. Se hace por `subscriber_code` (no depende de que exista un
    # `User` Django con ese email -- p. ej. reset hecho antes de tener
    # ningún login social/portal asociado).
    revoke_all_device_sessions_for_subscriber(subscriber_code, reason="password_changed")

    # Aviso por correo de "contraseña actualizada" -- cubre los tres
    # flujos que llaman a esta función (cambio autenticado, endpoint
    # legacy, y confirmación de "olvidé mi contraseña"). No debe poder
    # romper el cambio de contraseña si el envío falla (ver
    # enqueue_password_changed_email, que ya se traga sus propias
    # excepciones).
    try:
        from wind.services.password_changed_email import enqueue_password_changed_email

        enqueue_password_changed_email(
            email=normalize_email(email),
            first_name=getattr(user, "first_name", "") if user else "",
            last_name=getattr(user, "last_name", "") if user else "",
        )
    except Exception:
        logger.warning("No se pudo encolar el aviso de contraseña actualizada para %s", email, exc_info=True)


def request_password_reset(email: str, reset_page_url: str) -> dict:
    """
    Solicita recuperación. Siempre devuelve mensaje genérico (no revela si el email existe).
    """
    email_norm = normalize_email(email)
    registry = SubscriberEmailRegistry.objects.filter(email__iexact=email_norm).first()
    subscriber_code = None

    if registry and registry.subscriber_code:
        subscriber_code = registry.subscriber_code
    else:
        # Fallback a ListOfSubscriber (usuarios sincronizados desde PanAccess/CRM sin registro local previo)
        from wind.models import ListOfSubscriber
        sub = ListOfSubscriber.objects.filter(emails__iexact=email_norm).exclude(status=ListOfSubscriber.STATUS_CLOSED).first()
        if sub and sub.code:
            subscriber_code = sub.code

    if subscriber_code:
        token = build_reset_token(subscriber_code, email_norm)
        separator = "&" if "?" in reset_page_url else "?"
        reset_link = f"{reset_page_url}{separator}t={token}"
        try:
            from wind.tasks import send_password_reset_email_task

            send_password_reset_email_task.delay(email_norm, reset_link)
        except Exception:
            logger.exception("No se pudo encolar email de recuperación para %s", email_norm)
    else:
        logger.info("Solicitud de reset para email no registrado: %s", email_norm)

    return {"success": True, "message": GENERIC_FORGOT_MESSAGE}


def confirm_password_reset(token: str, new_pass: str) -> dict:
    """Valida token, resetea en PanAccess y sincroniza credenciales locales."""
    if is_reset_token_used(token):
        return {
            "success": False,
            "error_type": "TokenUsed",
            "message": "Este enlace ya fue utilizado. Solicita uno nuevo.",
        }

    try:
        subscriber_code, email = parse_reset_token(token)
    except SignatureExpired:
        return {
            "success": False,
            "error_type": "TokenExpired",
            "message": "Este enlace expiró. Solicita uno nuevo.",
        }
    except BadSignature:
        return {
            "success": False,
            "error_type": "InvalidToken",
            "message": "Enlace inválido o incompleto.",
        }

    registry = SubscriberEmailRegistry.objects.filter(
        subscriber_code=subscriber_code,
        email__iexact=email,
    ).first()
    if not registry:
        return {
            "success": False,
            "error_type": "InvalidToken",
            "message": "Enlace inválido o incompleto.",
        }

    try:
        reset_password_in_panaccess(subscriber_code, new_pass)
        sync_password_locally(subscriber_code, email, new_pass)
        mark_reset_token_used(token)
    except PanAccessConnectionError as e:
        # PanAccess no respondió -- esto sí es "intenta más tarde", a
        # diferencia de un rechazo por política (ver PanAccessAPIError
        # abajo). Mismo criterio que profile_password_view.
        logger.error("PanAccess sin conexión en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": "PanAccessConnectionError",
            "code": "panaccess_unavailable",
            "message": str(e),
        }
    except PanAccessTimeoutError as e:
        logger.error("Timeout de PanAccess en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": "PanAccessTimeoutError",
            "code": "panaccess_timeout",
            "message": str(e),
        }
    except (PanAccessAuthenticationError, PanAccessSessionError) as e:
        # Problema de la sesión/credencial de servicio con PanAccess, no
        # del usuario ni de la contraseña que mandó.
        logger.error("Error de sesión/credencial PanAccess en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": type(e).__name__,
            "code": "panaccess_integration_error",
            "message": str(e),
        }
    except PanAccessRateLimitError as e:
        logger.warning("Rate limit de PanAccess en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": "PanAccessRateLimitError",
            "code": "rate_limited",
            "message": str(e),
        }
    except PanAccessAPIError as e:
        # PanAccess respondió y rechazó el valor (p. ej. la misma política
        # de contraseña que ya se valida en el serializer, pero puede
        # haber alguna regla que esa validación local no anticipe) --
        # error de input del cliente (400), no de servidor. Antes esto
        # caía en el except PanAccessException genérico de abajo y salía
        # como 502 con un mensaje aplanado que descartaba el motivo real.
        logger.error("PanAccess rechazó la contraseña en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": "PanAccessAPIError",
            "code": "password_rejected_by_panaccess",
            "panaccess_error_code": getattr(e, "error_code", None),
            "message": str(e),
        }
    except PanAccessException as e:
        # Base no prevista específicamente -- se mantiene el mensaje
        # genérico de antes (no exponer detalle interno no clasificado).
        logger.error("Error PanAccess no clasificado en confirm_password_reset: %s", e)
        return {
            "success": False,
            "error_type": "PanAccessException",
            "message": "No se pudo restablecer la contraseña. Intenta de nuevo.",
        }
    except Exception:
        logger.exception("Error inesperado en confirm_password_reset")
        return {
            "success": False,
            "error_type": "Exception",
            "message": "No se pudo restablecer la contraseña. Intenta de nuevo.",
        }

    return {
        "success": True,
        "message": "Contraseña actualizada correctamente. Ya puedes iniciar sesión.",
    }
