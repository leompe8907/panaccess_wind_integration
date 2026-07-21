import hmac
import json
import hashlib
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from wind.models import (
    UDIDAuthRequest,
    SubscriberInfo,
    AppCredentials,
    EncryptedCredentialsLog,
)
from wind.utils.crypto_tv import hybrid_encrypt_for_app, hybrid_encrypt_for_device_public_key
from wind.utils.log_buffer import log_audit_async
from wind.utils.websocket_utils import check_udid_rate_limit, increment_rate_limit_counter

logger = logging.getLogger(__name__)

FATAL_CODES = {
    "invalid_udid",
    "invalid_temp_token",
    "expired",
    "subscriber_not_found",
    "no_device_public_key",
    "no_app_credentials",
    "encryption_failed",
}

# Códigos de `associate_udid_after_social_login` (Fase 2) que el frontend
# puede tratar como definitivos para ese intento de pareo (no vale la pena
# reintentar sin generar un nuevo QR/udid).
SOCIAL_PAIRING_FATAL_CODES = {
    "missing_params",
    "invalid_udid",
    "invalid_temp_token",
    "expired",
    "not_pending",
}


def compute_encrypted_hash(encrypted_data: str) -> str:
    return hashlib.sha256(encrypted_data.encode()).hexdigest()


def json_serialize_credentials(credentials_dict: dict) -> str:
    return json.dumps(credentials_dict)


def authenticate_with_udid_service(
    *,
    udid: str,
    temp_token: str = "",
    app_type: str,
    app_version: str,
    client_ip: str = "",
    user_agent: str = "",
) -> dict:
    """
    Lógica de negocio para acreditar por UDID y emitir credenciales cifradas.
    Retorna dict serializable a JSON.
    """
    if not udid:
        return {"ok": False, "error": "UDID is required", "code": "missing_udid"}

    try:
        with transaction.atomic():
            # 1) Lock del request por UDID (evita condiciones de carrera)
            try:
                req = UDIDAuthRequest.objects.select_for_update().get(udid=udid)
            except UDIDAuthRequest.DoesNotExist:
                return {"ok": False, "error": "Invalid UDID", "code": "invalid_udid"}

            # 1.5) temp_token obligatorio -- el udid por sí solo (8 hex,
            # ~4 mil millones de combinaciones) ya no alcanza como
            # credencial completa del flujo (ver auditoría).
            if not hmac.compare_digest(temp_token or "", req.temp_token or ""):
                return {
                    "ok": False,
                    "error": "Invalid temp_token",
                    "code": "invalid_temp_token",
                }

            # 2) Expiración y estado
            if req.is_expired():
                if req.status != "expired":
                    req.status = "expired"
                    req.save(update_fields=["status"])
                
                log_audit_async(
                    action_type="udid_validated",
                    subscriber_code=getattr(req, "subscriber_code", None),
                    udid=udid,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    details={"status": "expired", "validation_successful": False},
                )
                return {"ok": False, "error": "UDID has expired", "code": "expired", "status": "expired"}

            if req.status != "validated":
                return {
                    "ok": False,
                    "error": f"UDID not valid. Status: {req.status}",
                    "code": "not_validated",
                    "status": req.status,
                }

            if not getattr(req, "subscriber_code", None):
                return {
                    "ok": False,
                    "error": "UDID validated but not associated to subscriber yet",
                    "code": "not_associated",
                    "status": "validated",
                }

            # 3) Subscriber asociado (si falta en BD, eso sí es fatal).
            # `req.sn` viene poblado en el flujo manual/operador (match
            # exacto, sin cambios de comportamiento -- sigue siendo fatal si
            # no coincide). En el pareo por login social (Fase 2, ver
            # `associate_udid_after_social_login`) no hay `sn` -- ahí se
            # toma cualquier `SubscriberInfo` disponible de ese
            # subscriber_code (la TV/app decide con cuál seguir, no el
            # backend -- decisión del cliente), priorizando la de actividad
            # más reciente.
            try:
                if req.sn:
                    subscriber = SubscriberInfo.objects.get(
                        subscriber_code=req.subscriber_code, sn=req.sn
                    )
                else:
                    subscriber = (
                        SubscriberInfo.objects.filter(subscriber_code=req.subscriber_code)
                        .order_by("-last_login", "-updated_at")
                        .first()
                    )
                    if subscriber is None:
                        raise SubscriberInfo.DoesNotExist()
            except SubscriberInfo.DoesNotExist:
                return {
                    "ok": False,
                    "error": "Subscriber info not found or mismatched SN",
                    "code": "subscriber_not_found",
                }

            # 4) Payload de credenciales
            credentials_payload = {
                "subscriber_code": subscriber.subscriber_code,
                "sn": subscriber.sn,
                "login1": subscriber.login1,
                "login2": subscriber.login2,
                "password": subscriber.get_password(),
                "pin": subscriber.get_pin(),
                "packages": subscriber.packages,
                "products": subscriber.products,
                "timestamp": timezone.now().isoformat(),
            }

            # 5) Resolver cómo cifrar. Dos caminos, para no romper al
            # integrador que ya depende del esquema viejo (ver auditoría:
            # "no elimines la capa de RSA/AES, se reutiliza para otro
            # cliente"):
            #   a) Llave efímera por pareo (nuevo, TV/QR de este proyecto):
            #      si el dispositivo mandó `device_public_key` al pedir el
            #      UDID, se cifra específicamente para esa llave.
            #   b) Llave estática por app_type (esquema histórico): si no
            #      hay `device_public_key`, se cae al comportamiento de
            #      siempre contra `AppCredentials`, sin cambios.
            app_credentials = None
            if req.device_public_key:
                try:
                    encrypted_result = hybrid_encrypt_for_device_public_key(
                        json_serialize_credentials(credentials_payload), req.device_public_key
                    )
                except Exception as e:
                    return {
                        "ok": False,
                        "error": "Encryption failed",
                        "code": "encryption_failed",
                        "details": str(e),
                    }
                encryption_method = "Hybrid AES-256 + RSA-OAEP (llave efímera por pareo)"
            else:
                try:
                    app_credentials = AppCredentials.objects.get(
                        app_type=app_type,
                        app_version=app_version,
                        is_active=True,
                        is_compromised=False,
                    )
                    if hasattr(app_credentials, "is_usable") and not app_credentials.is_usable():
                        raise AppCredentials.DoesNotExist()
                except AppCredentials.DoesNotExist:
                    app_credentials = (
                        AppCredentials.objects.filter(
                            app_type=app_type, is_active=True, is_compromised=False
                        )
                        .order_by("-created_at")
                        .first()
                    )
                    if not app_credentials:
                        return {
                            "ok": False,
                            "error": f"No valid app credentials available for app_type='{app_type}'",
                            "code": "no_app_credentials",
                        }

                try:
                    encrypted_result = hybrid_encrypt_for_app(
                        json_serialize_credentials(credentials_payload), app_type
                    )
                except Exception as e:
                    return {
                        "ok": False,
                        "error": "Encryption failed",
                        "code": "encryption_failed",
                        "details": str(e),
                    }
                encryption_method = "Hybrid AES-256 + RSA-OAEP"

            # 6) Marcar request como entregado/used y auditar
            # Nota: antes esto se asignaba pero nunca se guardaba de verdad
            # -- tanto mark_credentials_delivered() como mark_as_used() (más
            # abajo) hacen save(update_fields=[...]) con una lista angosta
            # que no incluye app_type/app_version, así que quedaban en None
            # en BD pese a asignarse acá (bug preexistente, sin impacto en
            # el log de auditoría porque ese usa las variables locales, no
            # req.app_type/req.app_version -- pero sí dejaba el campo
            # persistido vacío). Se guarda explícito antes de continuar.
            req.app_type = app_type
            req.app_version = app_version
            update_fields = ["app_type", "app_version"]
            if not req.sn and subscriber.sn:
                # Pareo por login social (Fase 2): req.sn quedó None al
                # asociar -- ahora que ya se resolvió qué SubscriberInfo se
                # usó, se rellena para que EncryptedCredentialsLog/auditoría
                # no queden con sn vacío para siempre. No pisa un sn real ya
                # puesto por el flujo manual/operador (ese entra con sn ya
                # seteado, este `if` no aplica).
                req.sn = subscriber.sn
                update_fields.append("sn")
            req.save(update_fields=update_fields)
            if app_credentials is not None:
                req.app_credentials_used = app_credentials
                req.mark_credentials_delivered(app_credentials)
            else:
                req.credentials_delivered = True
                req.encryption_successful = True
                req.save(update_fields=["credentials_delivered", "encryption_successful"])
            req.mark_as_used()

            log_audit_async(
                action_type="udid_used",
                udid=req.udid,
                subscriber_code=req.subscriber_code,
                client_ip=client_ip,
                user_agent=user_agent,
                details={
                    "sn_assigned": req.sn,
                    "app_type": app_type,
                    "app_version": app_version,
                    "encryption_method": encryption_method,
                    "key_fingerprint": getattr(app_credentials, "key_fingerprint", None),
                    "status": req.status,
                    "validation_successful": True,
                },
            )

            # 7) Log del hash del cifrado (app_credentials nullable -- ver migración 0007)
            encrypted_hash = compute_encrypted_hash(encrypted_result["encrypted_data"])
            EncryptedCredentialsLog.objects.create(
                udid=req.udid,
                subscriber_code=req.subscriber_code,
                sn=req.sn,
                app_type=app_type,
                app_version=app_version,
                app_credentials=app_credentials,
                encrypted_data_hash=encrypted_hash,
                client_ip=client_ip,
                user_agent=user_agent,
                delivered_successfully=True,
            )

            # 8) Respuesta unificada
            return {
                "ok": True,
                "encrypted_credentials": encrypted_result,
                "security_info": {
                    "encryption_method": encryption_method,
                    "app_type": app_type,
                    "app_version": app_version,
                },
                "expires_at": req.expires_at.isoformat() if getattr(req, "expires_at", None) else None,
            }

    except Exception as e:
        return {"ok": False, "error": "Internal server error", "code": "internal_error", "details": str(e)}


def _notify_udid_validated(udid: str) -> None:
    """Avisa por WebSocket (grupo `udid_{udid}`) que el pareo quedó validado.
    Mismo patrón que `ValidateAndAssociateUDIDView` (flujo manual/operador) --
    se llama vía `transaction.on_commit` para no notificar si el commit falla."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"udid_{udid}", {"type": "udid.validated", "udid": udid}
            )
        else:
            logger.warning("Channel layer no disponible; no se notificó udid %s", udid)
    except Exception:
        logger.exception("Error notificando WebSocket para udid %s", udid)


def associate_udid_after_social_login(
    *,
    udid: str,
    temp_token: str,
    subscriber_code: str,
    client_ip: str = "",
    user_agent: str = "",
) -> dict:
    """
    Asocia un `UDIDAuthRequest` a un suscriptor resuelto vía login social
    (Google/Facebook) -- Fase 2 del pareo TV, camino "solo autorizar la TV"
    (decisión del cliente 2026-07-21): el celular que escanea el QR nunca
    recibe el password real de PanAccess, solo confirma que la TV quedó
    autorizada; el password viaja cifrado backend->TV cuando la TV llama a
    `authenticate_with_udid_service` (arriba), reutilizando exactamente el
    mismo mecanismo de Fase 1.

    A diferencia del flujo manual/operador (`ValidateAndAssociateUDIDView`),
    acá NO se exige ni valida `sn` (smartcard) -- ese campo puede ser None
    para este `UDIDAuthRequest`; PanAccess resuelve la SN por su cuenta
    cuando la TV hace el login real con las credenciales entregadas (mismo
    principio ya documentado en Fase 1). Tampoco se marca `used_at`/
    status="used" acá -- eso sigue pasando solo cuando la TV efectivamente
    consume las credenciales.
    """
    if not udid or not subscriber_code:
        return {
            "ok": False,
            "code": "missing_params",
            "error": "udid and subscriber_code are required",
        }

    # Rate limit por udid (1/min, mismo umbral que `ValidateAndAssociateUDIDView`
    # en el flujo manual/operador) -- este camino nuevo es alcanzable vía
    # /auth/google//auth/facebook/, que solo tienen un throttle genérico por
    # IP (20/min), no uno por udid; sin esto, un atacante con un access_token
    # social válido podría probar udids/temp_tokens a ese ritmo más alto.
    is_allowed, _remaining, retry_after = check_udid_rate_limit(
        udid, max_requests=1, window_minutes=1
    )
    if not is_allowed:
        return {
            "ok": False,
            "code": "rate_limited",
            "error": "Too many attempts for this udid",
            "retry_after": retry_after,
        }

    try:
        with transaction.atomic():
            try:
                req = UDIDAuthRequest.objects.select_for_update().get(udid=udid)
            except UDIDAuthRequest.DoesNotExist:
                return {"ok": False, "code": "invalid_udid", "error": "Invalid UDID"}

            if not hmac.compare_digest(temp_token or "", req.temp_token or ""):
                return {
                    "ok": False,
                    "code": "invalid_temp_token",
                    "error": "Invalid temp_token",
                }

            if req.is_expired():
                if req.status != "expired":
                    req.status = "expired"
                    req.save(update_fields=["status"])
                return {"ok": False, "code": "expired", "error": "UDID has expired"}

            if req.status != "pending":
                return {
                    "ok": False,
                    "code": "not_pending",
                    "error": f"UDID not pending. Status: {req.status}",
                    "status": req.status,
                }

            now = timezone.now()
            req.subscriber_code = subscriber_code
            req.status = "validated"
            req.validated_at = now
            req.validated_by_operator = "social_login"
            req.method = "automatic"
            req.client_ip = client_ip
            req.user_agent = user_agent
            req.save(
                update_fields=[
                    "subscriber_code",
                    "status",
                    "validated_at",
                    "validated_by_operator",
                    "method",
                    "client_ip",
                    "user_agent",
                ]
            )

            log_audit_async(
                action_type="udid_validated",
                udid=req.udid,
                subscriber_code=subscriber_code,
                client_ip=client_ip,
                user_agent=user_agent,
                details={
                    "validation_method": "social_login",
                    "validation_successful": True,
                },
            )

            transaction.on_commit(lambda: _notify_udid_validated(udid))

        increment_rate_limit_counter("udid", udid)
        return {"ok": True, "udid": udid, "subscriber_code": subscriber_code}

    except Exception as e:
        return {
            "ok": False,
            "code": "internal_error",
            "error": "Internal server error",
            "details": str(e),
        }
