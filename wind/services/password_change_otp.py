"""
Cambiar contraseña con código OTP por correo (2026-09-14).

Flujo (usuario ya autenticado, desde Mi Cuenta -- distinto del "olvidé mi
contraseña" en wind/services/password_reset.py, que es para usuarios sin
sesión):

  1. request_password_change_otp(subscriber_code, user) -> genera un código
     de 6 dígitos, lo guarda hasheado con expiración, lo manda por correo.
  2. check_password_change_otp(subscriber_code, otp_code) -> valida el
     código (sin consumirlo todavía -- ver más abajo por qué).
  3. Quien llama (wind/api/profile/views.py) aplica el cambio real en
     PanAccess + sync_password_locally, y recién ahí llama a
     consume_password_change_otp(record) para marcarlo usado.

No reemplaza al flujo de `oldPass` (profile_password_view) -- ambos
endpoints coexisten a propósito, ver docs/CAMBIO_CONTRASENA_OTP_2026-09-14.md.
"""
from __future__ import annotations

import hashlib
import logging
import secrets

from django.utils import timezone

from appConfig import PasswordChangeOtpConfig
from wind.models import PasswordChangeOtp

logger = logging.getLogger(__name__)


def generate_otp_code() -> str:
    """Código numérico de 6 dígitos, con ceros a la izquierda si aplica."""
    n = secrets.randbelow(10 ** PasswordChangeOtpConfig.CODE_LENGTH)
    return str(n).zfill(PasswordChangeOtpConfig.CODE_LENGTH)


def _hash_code(subscriber_code: str, code: str) -> str:
    # Salado con el propio subscriber_code -- alcanza para que el hash no
    # sea reutilizable entre cuentas distintas si dos usuarios recibieran
    # por coincidencia el mismo código de 6 dígitos; no hace falta un
    # secreto adicional porque el código en sí ya es corto (6 dígitos) y de
    # un solo uso, protegido por expiración + límite de intentos, no por el
    # hash en sí.
    digest = hashlib.sha256(f"{subscriber_code}:{code}".encode("utf-8")).hexdigest()
    return digest


def mask_email(email: str) -> str:
    """
    "juan.perez@gmail.com" -> "jua*******@gmail.com". Si el email es
    inválido o vacío, devuelve "" (el caller decide qué mostrar).
    """
    email = (email or "").strip()
    if "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        visible = local[:1]
    else:
        visible = local[:3]
    hidden = "*" * max(3, len(local) - len(visible))
    return f"{visible}{hidden}@{domain}"


def _active_otp_queryset(subscriber_code: str):
    return PasswordChangeOtp.objects.filter(
        subscriber_code=subscriber_code,
        consumed_at__isnull=True,
    )


def request_password_change_otp(*, subscriber_code: str, email: str) -> dict:
    """
    Genera y encola el código. Invalida cualquier código previo sin usar
    para este suscriptor (evita que queden varios códigos "vivos" a la vez
    -- solo el último enviado debe funcionar).

    Devuelve {"success": True, "masked_email": "...", "expires_in_minutes": N}
    o {"success": False, "code": "otp_cooldown", ...} si se pidió otro
    código hace muy poco (PasswordChangeOtpConfig.REQUEST_COOLDOWN_SECONDS).
    """
    if not email:
        return {
            "success": False,
            "code": "no_email",
            "message": "Tu cuenta no tiene un correo registrado para enviar el código.",
        }

    now = timezone.now()
    last = _active_otp_queryset(subscriber_code).order_by("-created_at").first()
    if last is not None:
        seconds_since_last = (now - last.created_at).total_seconds()
        if seconds_since_last < PasswordChangeOtpConfig.REQUEST_COOLDOWN_SECONDS:
            wait_seconds = int(PasswordChangeOtpConfig.REQUEST_COOLDOWN_SECONDS - seconds_since_last)
            return {
                "success": False,
                "code": "otp_cooldown",
                "wait_seconds": wait_seconds,
                "message": "Ya te enviamos un código hace poco. Espera un momento antes de pedir otro.",
            }

    # Invalida los códigos previos sin usar -- solo el nuevo debe ser válido.
    _active_otp_queryset(subscriber_code).update(consumed_at=now)

    code = generate_otp_code()
    PasswordChangeOtp.objects.create(
        subscriber_code=subscriber_code,
        code_hash=_hash_code(subscriber_code, code),
        expires_at=now + timezone.timedelta(minutes=PasswordChangeOtpConfig.EXPIRY_MINUTES),
    )

    try:
        from wind.services.password_change_otp_email import enqueue_password_change_otp_email

        enqueue_password_change_otp_email(
            email=email,
            code=code,
            subscriber_code=subscriber_code,
            expiry_minutes=PasswordChangeOtpConfig.EXPIRY_MINUTES,
        )
    except Exception:
        logger.exception("No se pudo encolar el correo de código OTP para %s", subscriber_code)
        return {
            "success": False,
            "code": "otp_email_failed",
            "message": "No se pudo enviar el código. Intenta de nuevo en unos segundos.",
        }

    logger.info("Código OTP de cambio de contraseña generado para %s", subscriber_code)
    return {
        "success": True,
        "masked_email": mask_email(email),
        "expires_in_minutes": PasswordChangeOtpConfig.EXPIRY_MINUTES,
        "message": "Te enviamos un código de verificación a tu correo.",
    }


def check_password_change_otp(*, subscriber_code: str, code: str) -> dict:
    """
    Valida el código sin consumirlo (ver docstring del módulo -- el
    consumo real lo hace consume_password_change_otp, después de aplicar
    el cambio en PanAccess con éxito).

    Devuelve {"success": True, "record": <PasswordChangeOtp>} o
    {"success": False, "code": "...", "message": "..."}.
    """
    now = timezone.now()
    record = (
        _active_otp_queryset(subscriber_code)
        .filter(expires_at__gt=now)
        .order_by("-created_at")
        .first()
    )
    if record is None:
        return {
            "success": False,
            "code": "otp_missing_or_expired",
            "message": "El código expiró o no se ha solicitado ninguno. Pide uno nuevo.",
        }

    if record.attempts >= PasswordChangeOtpConfig.MAX_VERIFY_ATTEMPTS:
        return {
            "success": False,
            "code": "otp_locked",
            "message": "Demasiados intentos fallidos con este código. Pide uno nuevo.",
        }

    expected_hash = _hash_code(subscriber_code, code)
    if not secrets.compare_digest(expected_hash, record.code_hash):
        record.attempts += 1
        record.save(update_fields=["attempts"])
        remaining = max(0, PasswordChangeOtpConfig.MAX_VERIFY_ATTEMPTS - record.attempts)
        return {
            "success": False,
            "code": "otp_incorrect",
            "attempts_remaining": remaining,
            "message": "El código no es correcto.",
        }

    return {"success": True, "record": record}


def consume_password_change_otp(record: PasswordChangeOtp) -> None:
    record.consumed_at = timezone.now()
    record.save(update_fields=["consumed_at"])
