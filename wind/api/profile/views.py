import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from wind.api.profile.serializers import ProfilePasswordSerializer, ProfileCloseAccountSerializer
from wind.exceptions import (
    PanAccessException,
    PanAccessAPIError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAuthenticationError,
    PanAccessSessionError,
    PanAccessRateLimitError,
)
from wind.permissions import IsOwnerSubscriber
from wind.services.password_reset import reset_password_in_panaccess, sync_password_locally
from wind.utils.password_policy import PASSWORD_POLICY_CODE
from wind.services.subscriber_closure import close_subscriber_account
from wind.services.subscriber_catalog import (
    build_subscriber_detail_payload,
    build_subscriber_products_payload,
    resolve_subscriber_code_for_user,
)
from wind.throttles import ProfileThrottle

from appConfig import FeatureConfig

logger = logging.getLogger(__name__)
User = get_user_model()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def profile_me_view(request):
    """Datos del suscriptor PanAccess vinculado al usuario autenticado."""
    subscriber_code = resolve_subscriber_code_for_user(request.user)
    if not subscriber_code:
        return Response(
            {
                "success": False,
                "message": "No hay suscriptor vinculado a este usuario.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    subscriber = build_subscriber_detail_payload(subscriber_code)
    if not subscriber:
        return Response(
            {
                "success": False,
                "message": "No se encontró información del suscriptor.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response({"success": True, "subscriber": subscriber})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOwnerSubscriber])
@throttle_classes([ProfileThrottle])
def profile_password_view(request):
    """
    Cambia la contraseña PanAccess del propio suscriptor.

    Status codes (ver auditoría "BACKEND_CHANGE_PASSWORD_VALIDATION_ISSUE"):
    400 si la contraseña no cumple la política (validada localmente en el
    serializer, o rechazada por PanAccess) -- antes salía como 502
    indistinguible de una falla real de conectividad. 502/503/504/429
    quedan reservados para lo que sí es un problema de la integración con
    PanAccess, no del valor que mandó el cliente.
    """
    ser = ProfilePasswordSerializer(data=request.data)
    if not ser.is_valid():
        payload = {"success": False, "errors": ser.errors}
        if "newPass" in ser.errors:
            payload["code"] = PASSWORD_POLICY_CODE
        return Response(payload, status=status.HTTP_400_BAD_REQUEST)

    code = ser.validated_data["code"]
    new_pass = ser.validated_data["newPass"]

    try:
        reset_password_in_panaccess(code, new_pass)
        sync_password_locally(code, request.user.email or "", new_pass)
        return Response(
            {
                "success": True,
                "message": "Contraseña actualizada",
            }
        )
    except PanAccessConnectionError as e:
        # PanAccess no respondió -- esto sí es "intenta más tarde".
        return Response(
            {"success": False, "error_type": "PanAccessConnectionError", "code": "panaccess_unavailable", "message": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except PanAccessTimeoutError as e:
        return Response(
            {"success": False, "error_type": "PanAccessTimeoutError", "code": "panaccess_timeout", "message": str(e)},
            status=status.HTTP_504_GATEWAY_TIMEOUT,
        )
    except (PanAccessAuthenticationError, PanAccessSessionError) as e:
        # Problema de la sesión/credencial de servicio con PanAccess, no
        # del usuario ni de la contraseña que mandó.
        return Response(
            {"success": False, "error_type": type(e).__name__, "code": "panaccess_integration_error", "message": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except PanAccessRateLimitError as e:
        return Response(
            {"success": False, "error_type": "PanAccessRateLimitError", "code": "rate_limited", "message": str(e)},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    except PanAccessAPIError as e:
        # PanAccess respondió y rechazó el valor (típicamente la misma
        # política de contraseña que ya se valida arriba, pero puede
        # haber reglas de PanAccess que esta validación local no
        # anticipe) -- es un error de input del cliente, no de servidor.
        return Response(
            {
                "success": False,
                "error_type": "PanAccessAPIError",
                "code": "password_rejected_by_panaccess",
                "panaccess_error_code": getattr(e, "error_code", None),
                "message": str(e),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except PanAccessException as e:
        # Base no prevista específicamente -- se mantiene 502 como antes
        # (comportamiento sin cambios para lo que no se pudo clasificar).
        return Response(
            {"success": False, "error_type": "PanAccessException", "message": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception:
        # No devolver str(e) al cliente -- puede filtrar detalles internos
        # (ver auditoría). El detalle real ya queda en el log de arriba.
        logger.exception("Error en profile_password_view")
        return Response(
            {
                "success": False,
                "message": "Ocurrió un error inesperado al cambiar la contraseña. Intenta de nuevo.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def profile_products_view(request):
    """
    Smartcards del suscriptor autenticado y productos asociados a cada una.
    """
    subscriber_code = resolve_subscriber_code_for_user(request.user)
    if not subscriber_code:
        return Response(
            {
                "success": False,
                "message": "No hay suscriptor vinculado a este usuario.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    payload = build_subscriber_products_payload(subscriber_code)
    return Response({"success": True, **payload})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def profile_subscriber_view(request):
    """Datos del suscriptor PanAccess vinculado."""
    subscriber_code = resolve_subscriber_code_for_user(request.user)
    if not subscriber_code:
        return Response(
            {
                "success": False,
                "message": "No hay suscriptor vinculado a este usuario.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    detail = build_subscriber_detail_payload(subscriber_code)
    if not detail:
        return Response(
            {
                "success": False,
                "message": "No se encontró información del suscriptor todavía. Se disparó una sincronización en segundo plano, intenta de nuevo en unos segundos.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(
        {
            "success": True,
            "subscriber_code": subscriber_code,
            "subscriber": detail,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOwnerSubscriber])
@throttle_classes([ProfileThrottle])
def profile_close_account_view(request):
    """
    Cierra la cuenta del suscriptor autenticado (desaprovisiona PanAccess + tombstone local).
    """
    if not FeatureConfig.CLOSE_SUBSCRIBER_DASHBOARD_ENABLED:
        return Response(
            {
                "success": False,
                "message": "El cierre de cuenta desde el dashboard está deshabilitado.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    from wind.utils.recaptcha import verify_recaptcha

    recaptcha_ok, recaptcha_error = verify_recaptcha(
        request.data.get("recaptcha_token"),
        remote_ip=request.META.get("REMOTE_ADDR"),
    )
    if not recaptcha_ok:
        return Response(
            {
                "success": False,
                "error_type": "RecaptchaFailed",
                "message": recaptcha_error or "Verificación reCAPTCHA fallida.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ser = ProfileCloseAccountSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"success": False, "errors": ser.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    code = ser.validated_data["code"].strip()
    dry_run = bool(ser.validated_data.get("dry_run"))
    reason = (ser.validated_data.get("reason") or "").strip() or "user_dashboard_close"

    from wind.models import ListOfSubscriber

    subscriber = ListOfSubscriber.objects.filter(code=code).first()
    if subscriber and subscriber.status == ListOfSubscriber.STATUS_CLOSED and not dry_run:
        return Response(
            {
                "success": True,
                "already_closed": True,
                "message": "Esta cuenta ya estaba cerrada.",
                "subscriber_code": code,
            },
        )

    try:
        result = close_subscriber_account(
            code,
            reason=reason,
            requested_by=request.user,
            dry_run=dry_run,
        )
        http_status = status.HTTP_200_OK if result.get("success") else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)
    except Exception:
        # No devolver str(e) al cliente -- puede filtrar detalles internos
        # (ver auditoría). El detalle real ya queda en el log de arriba.
        logger.exception("Error en profile_close_account_view para %s", code)
        return Response(
            {
                "success": False,
                "message": "Ocurrió un error inesperado al eliminar la cuenta. Intenta de nuevo.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
