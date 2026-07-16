import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from wind.api.profile.serializers import ProfilePasswordSerializer, ProfileCloseAccountSerializer
from wind.exceptions import PanAccessException
from wind.permissions import IsOwnerSubscriber
from wind.services.password_reset import reset_password_in_panaccess, sync_password_locally
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
    """Cambia la contraseña PanAccess del propio suscriptor."""
    ser = ProfilePasswordSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"success": False, "errors": ser.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

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
    except PanAccessException as e:
        return Response(
            {"success": False, "error_type": "PanAccessException", "message": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception as e:
        logger.exception("Error en profile_password_view")
        return Response(
            {"success": False, "message": str(e)},
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
    except Exception as e:
        logger.exception("Error en profile_close_account_view para %s", code)
        return Response(
            {"success": False, "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
