import json
import hashlib
from django.db import transaction
from django.utils import timezone

from wind.models import (
    UDIDAuthRequest,
    SubscriberInfo,
    AppCredentials,
    EncryptedCredentialsLog,
)
from wind.utils.crypto_tv import hybrid_encrypt_for_app
from wind.utils.log_buffer import log_audit_async

FATAL_CODES = {
    "invalid_udid",
    "expired",
    "subscriber_not_found",
    "no_app_credentials",
    "encryption_failed",
}


def compute_encrypted_hash(encrypted_data: str) -> str:
    return hashlib.sha256(encrypted_data.encode()).hexdigest()


def json_serialize_credentials(credentials_dict: dict) -> str:
    return json.dumps(credentials_dict)


def authenticate_with_udid_service(
    *,
    udid: str,
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

            if not getattr(req, "subscriber_code", None) or not getattr(req, "sn", None):
                return {
                    "ok": False,
                    "error": "UDID validated but not associated to subscriber yet",
                    "code": "not_associated",
                    "status": "validated",
                }

            # 3) Subscriber asociado (si falta en BD, eso sí es fatal)
            try:
                subscriber = SubscriberInfo.objects.get(
                    subscriber_code=req.subscriber_code, sn=req.sn
                )
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

            # 5) AppCredentials: 1º intento exacto por (app_type, app_version), activo y usable
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
                # Fallback: última activa, no comprometida, por app_type
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

            # 6) Cifrado híbrido
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

            # 7) Marcar request como entregado/used y auditar
            req.app_type = app_type
            req.app_version = app_version
            req.app_credentials_used = app_credentials
            req.mark_credentials_delivered(app_credentials)
            req.mark_as_used()

            # Log de auditoría
            log_audit_async(
                action_type="udid_used",
                udid=req.udid,
                subscriber_code=req.subscriber_code,
                client_ip=client_ip,
                user_agent=user_agent,
                details={
                    "sn_assigned": req.sn,
                    "app_type": app_type,
                    "app_version": getattr(app_credentials, "app_version", app_version),
                    "encryption_method": "Hybrid AES-256 + RSA-OAEP",
                    "key_fingerprint": getattr(app_credentials, "key_fingerprint", None),
                    "status": req.status,
                    "validation_successful": True,
                },
            )

            # 8) Log del hash del cifrado
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

            # 9) Respuesta unificada
            return {
                "ok": True,
                "encrypted_credentials": encrypted_result,
                "security_info": {
                    "encryption_method": "Hybrid AES-256 + RSA-OAEP",
                    "app_type": app_type,
                    "app_version": getattr(app_credentials, "app_version", app_version),
                },
                "expires_at": req.expires_at.isoformat() if getattr(req, "expires_at", None) else None,
            }

    except Exception as e:
        return {"ok": False, "error": "Internal server error", "code": "internal_error", "details": str(e)}
