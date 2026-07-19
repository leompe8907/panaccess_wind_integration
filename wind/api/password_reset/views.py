from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from wind.api.password_reset.serializers import (
    ForgotPasswordSerializer,
    ResetPasswordConfirmSerializer,
)
from wind.services.password_reset import confirm_password_reset, request_password_reset
from wind.throttles import PasswordResetThrottle


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_forgot_view(request):
    """
    Solicita recuperación de contraseña por email.
    Respuesta genérica siempre (no revela si el correo existe).
    """
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

    ser = ForgotPasswordSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"success": False, "errors": ser.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reset_page_url = request.build_absolute_uri("/wind/reset-password/")
    result = request_password_reset(ser.validated_data["email"], reset_page_url)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_confirm_view(request):
    """Confirma nueva contraseña con token del enlace de recuperación."""
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

    ser = ResetPasswordConfirmSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"success": False, "errors": ser.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = confirm_password_reset(
        ser.validated_data["token"],
        ser.validated_data["newPass"],
    )
    if not result.get("success"):
        error_type = result.get("error_type", "")
        if error_type in ("TokenExpired", "TokenUsed", "InvalidToken"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        if error_type == "PanAccessException":
            return Response(result, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(result, status=status.HTTP_200_OK)
