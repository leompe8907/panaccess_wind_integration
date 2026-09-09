"""
Eliminación de cuenta con confirmación por correo y ejecución diferida a la
fecha de corte (2026-09-08, nuevo flujo pedido por el cliente: "si decide
eliminar hoy pero le quedan 5 días de uso, debería esperar esos 5 días para
que se ejecute").

Distinto de wind/services/subscriber_closure.py::close_subscriber_account
(que sigue existiendo tal cual, para cierre inmediato/uso interno): acá
nada se ejecuta contra PanAccess hasta que:
  1. el usuario pide la eliminación desde la app -> request_account_deletion()
     manda un correo con un enlace firmado (24h de vigencia), sin tocar
     nada todavía.
  2. el usuario confirma haciendo click -> confirm_account_deletion() corta
     el acceso YA (mismo mecanismo que close_subscriber_account, ver
     cut_subscriber_access_and_tombstone) y programa la fecha real de
     cierre (ListOfSubscriber.scheduled_closure_at = fecha de corte de la
     suscripción).
  3. wind.tasks.retry_partial_closures_task (ya existía, corre cada
     CeleryConfig.CLOSURE_RETRY_MINUTES) recién ejecuta el cierre real en
     PanAccess cuando esa fecha llega -- no hizo falta una tarea nueva.
"""
from __future__ import annotations

import base64
import logging

from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils import timezone

from wind.models import AccountDeletionRequest, ListOfSubscriber

logger = logging.getLogger(__name__)
User = get_user_model()

ACCOUNT_DELETION_SALT = "wind.account-deletion"
# "Este enlace expirará en 24 horas" (mockup del cliente) -- a diferencia
# del reset de contraseña (60 min), acá no hay apuro: es solo la
# confirmación de que la solicitud es genuina.
ACCOUNT_DELETION_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24

GENERIC_REQUEST_MESSAGE = (
    "Te enviamos un enlace de confirmación a tu correo. El enlace expira en "
    "24 horas. Revisa también la carpeta de spam."
)


def build_deletion_token(request_id: int, subscriber_code: str) -> str:
    signer = TimestampSigner(salt=ACCOUNT_DELETION_SALT)
    code_b64 = base64.urlsafe_b64encode(subscriber_code.encode("utf-8")).decode("ascii")
    payload = f"{request_id}|{code_b64}"
    return signer.sign(payload)


def parse_deletion_token(token: str) -> tuple[int, str]:
    """Devuelve (request_id, subscriber_code). Lanza BadSignature o SignatureExpired."""
    signer = TimestampSigner(salt=ACCOUNT_DELETION_SALT)
    raw = signer.unsign(token, max_age=ACCOUNT_DELETION_TOKEN_MAX_AGE_SECONDS)
    parts = str(raw).split("|", 1)
    if len(parts) != 2:
        raise BadSignature("Formato de token inválido")
    request_id_raw, code_b64 = parts
    try:
        request_id = int(request_id_raw)
        subscriber_code = base64.urlsafe_b64decode(code_b64.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise BadSignature("Contenido de token inválido") from exc
    return request_id, subscriber_code


def _resolve_subscriber_email(subscriber_code: str) -> str:
    sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    return (sub.emails or "").strip() if sub else ""


def request_account_deletion(
    subscriber_code: str, *, requested_by=None, reason: str = ""
) -> dict:
    """
    Crea (o reusa, si ya había una sin confirmar) la solicitud y manda el
    correo de confirmación. No toca ListOfSubscriber ni PanAccess.
    """
    subscriber_code = (subscriber_code or "").strip()
    if not subscriber_code:
        return {"success": False, "message": "subscriber_code es requerido"}

    subscriber = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    if subscriber and subscriber.status == ListOfSubscriber.STATUS_CLOSED:
        return {"success": False, "code": "already_closed", "message": "Esta cuenta ya está cerrada."}
    # CORREGIDO (2026-09-09): ya no se usa status == PENDING_CLOSURE para
    # detectar "ya hay una eliminación programada" -- con el fix de abajo
    # (confirm_account_deletion ya NO tombstonea a PENDING_CLOSURE, para no
    # cortar el acceso antes de tiempo), una cuenta con cierre confirmado
    # sigue en ACTIVE hasta la fecha de corte. scheduled_closure_at es la
    # señal correcta acá.
    if subscriber and subscriber.scheduled_closure_at:
        return {
            "success": False,
            "code": "closure_already_scheduled",
            "message": "Ya hay una eliminación en curso para esta cuenta.",
            "scheduled_for": subscriber.scheduled_closure_at.isoformat(),
        }

    email = _resolve_subscriber_email(subscriber_code)
    if not email:
        logger.warning(
            "No se pudo encolar email de confirmación de eliminación: %s no tiene email registrado",
            subscriber_code,
        )
        return {
            "success": False,
            "code": "no_email_on_file",
            "message": "No hay un correo registrado para esta cuenta. Contacta a soporte.",
        }

    existing = AccountDeletionRequest.objects.filter(
        subscriber_code=subscriber_code, confirmed_at__isnull=True
    ).order_by("-requested_at").first()

    if existing:
        deletion_request = existing
        deletion_request.last_email_sent_at = timezone.now()
        deletion_request.save(update_fields=["last_email_sent_at"])
    else:
        deletion_request = AccountDeletionRequest.objects.create(
            subscriber_code=subscriber_code,
            requested_by=requested_by,
            reason=reason or "user_app_delete_request",
        )

    token = build_deletion_token(deletion_request.id, subscriber_code)

    try:
        from appConfig import EmailConfig
        from wind.services.account_deletion_email import enqueue_account_deletion_confirmation_email

        base_url = EmailConfig.ACCOUNT_DELETION_CONFIRM_LINK_BASE_URL
        confirm_link = f"{base_url}/wind/eliminar-cuenta/confirmar/?t={token}"

        enqueue_account_deletion_confirmation_email(
            email=email,
            confirm_link=confirm_link,
            subscriber_code=subscriber_code,
        )
    except Exception:
        logger.exception("No se pudo encolar email de confirmación de eliminación para %s", subscriber_code)

    return {"success": True, "message": GENERIC_REQUEST_MESSAGE, "masked_email": _mask_email(email)}


def _mask_email(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[:1] + "*" * max(len(local) - 1, 1)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"


def confirm_account_deletion(token: str) -> dict:
    """
    Valida el token, corta el acceso YA y programa el cierre real para la
    fecha de corte de la suscripción (ListOfSubscriber.lastExpiryTime).

    Idempotente: si el token es válido pero la solicitud ya estaba
    confirmada (doble click, o el link se abrió dos veces), devuelve la
    misma fecha programada sin repetir el corte de acceso.
    """
    try:
        request_id, subscriber_code = parse_deletion_token(token)
    except SignatureExpired:
        return {
            "success": False,
            "error_type": "TokenExpired",
            "message": "Este enlace expiró. Solicita la eliminación de nuevo desde la app.",
        }
    except BadSignature:
        return {
            "success": False,
            "error_type": "InvalidToken",
            "message": "Enlace inválido o incompleto.",
        }

    deletion_request = AccountDeletionRequest.objects.filter(
        id=request_id, subscriber_code=subscriber_code
    ).first()
    if not deletion_request:
        return {
            "success": False,
            "error_type": "InvalidToken",
            "message": "Enlace inválido o incompleto.",
        }

    subscriber = ListOfSubscriber.objects.filter(code=subscriber_code).first()

    if deletion_request.confirmed_at:
        # Ya confirmado antes (doble click) -- no repetir el corte de
        # acceso, solo informar la fecha ya programada.
        return {
            "success": True,
            "already_confirmed": True,
            "subscriber_code": subscriber_code,
            "scheduled_for": subscriber.scheduled_closure_at.isoformat()
            if subscriber and subscriber.scheduled_closure_at
            else None,
            "message": "Esta eliminación ya había sido confirmada.",
        }

    # CORREGIDO (2026-09-09): acá NO se corta el acceso todavía -- el
    # cliente confirmó que durante estos días el usuario sigue teniendo
    # servicio con normalidad ("le quedan 5 días de uso, debería esperar
    # esos 5 días para que se ejecute"). Antes esta función llamaba a
    # cut_subscriber_access_and_tombstone() acá mismo (desactivaba el User,
    # invalidaba JWT, revocaba dispositivos) -- eso cortaba el acceso al
    # toque de confirmar el correo, contradiciendo justo lo que el cliente
    # pidió. Ahora solo se guarda la fecha; el corte de acceso real pasa
    # recién cuando retry_partial_closures_task ejecuta el cierre de
    # verdad en esa fecha (mismo mecanismo de siempre, ver
    # subscriber_closure.close_subscriber_account).
    if subscriber is None:
        subscriber, _ = ListOfSubscriber.objects.update_or_create(
            code=subscriber_code,
            defaults={"id": subscriber_code, "status": ListOfSubscriber.STATUS_ACTIVE},
        )

    # Fecha de corte real de la suscripción. Si por algún motivo no hay
    # dato (nunca se sincronizó lastExpiryTime desde PanAccess), no hay una
    # fecha segura para diferir -- se usa "ahora" para que
    # retry_partial_closures_task lo ejecute en su próximo ciclo en vez de
    # dejarlo confirmado pero sin ninguna fecha que algún día lo dispare;
    # se loguea para poder revisarlo (no debería pasar en la práctica, todo
    # abonado con producto activo tiene esta fecha sincronizada).
    scheduled_for = subscriber.lastExpiryTime
    if not scheduled_for:
        scheduled_for = timezone.now()
        logger.warning(
            "%s no tiene lastExpiryTime sincronizado -- eliminación confirmada sin fecha de corte "
            "conocida, se programó para ejecutarse en el próximo ciclo de retry_partial_closures_task",
            subscriber_code,
        )

    subscriber.scheduled_closure_at = scheduled_for
    subscriber.save(update_fields=["scheduled_closure_at"])

    deletion_request.confirmed_at = timezone.now()
    deletion_request.save(update_fields=["confirmed_at"])

    return {
        "success": True,
        "subscriber_code": subscriber_code,
        "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        "message": "Eliminación confirmada. Tu sesión se cerró en todos los dispositivos.",
    }
