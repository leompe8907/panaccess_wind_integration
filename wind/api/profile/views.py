import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from wind.api.profile.serializers import ProfilePasswordSerializer
from wind.exceptions import PanAccessException
from wind.permissions import IsOwnerSubscriber
from wind.services import get_panaccess
from wind.services.subscriber_catalog import (
    build_subscriber_detail_payload,
    build_subscriber_products_payload,
    resolve_subscriber_code_for_user,
)
from wind.throttles import ProfileThrottle

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
        panaccess = get_panaccess()
        response = panaccess.call(
            "resetSubscriberPassword",
            {"code": code, "newPass": new_pass, "hash": False},
        )
        if response.get("success"):
            return Response(
                {
                    "success": True,
                    "message": "Contraseña actualizada",
                    "result": response.get("answer", response),
                }
            )
        raise PanAccessException(response.get("errorMessage", "Error al cambiar contraseña"))
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
                "message": "No se encontró información del suscriptor. Ejecuta sincronización.",
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
