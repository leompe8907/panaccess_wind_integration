import hmac
import logging

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from appConfig import AppLogsConfig
from applogs.serializers import LogEventIngestSerializer
from applogs.services import record_log_event
from wind.services.subscriber_catalog import resolve_subscriber_code_for_user
from wind.throttles import LogIngestThrottle
from wind.utils.websocket_utils import get_client_ip

logger = logging.getLogger(__name__)


def _valid_api_key(request) -> bool:
    expected = AppLogsConfig.INGEST_API_KEY
    if not expected:
        return False
    provided = request.headers.get("X-App-Log-Key", "")
    return hmac.compare_digest(provided, expected)


def _resolve_subscriber_code_best_effort(request) -> str:
    """
    JWT opcional: este endpoint debe poder capturar errores de ANTES del
    login (ej. un crash en la pantalla de login), así que un token
    ausente, vencido o inválido nunca debe rechazar el request -- solo se
    intenta resolver el suscriptor si hay un token válido, y se sigue sin
    él en cualquier otro caso.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return ""
    try:
        from wind.services.jwt_invalidation import PasswordAwareJWTAuthentication

        result = PasswordAwareJWTAuthentication().authenticate(request)
        if not result:
            return ""
        user, _token = result
        return resolve_subscriber_code_for_user(user) or ""
    except Exception:
        return ""


@api_view(["POST"])
@authentication_classes([])  # auth manual y best-effort, ver _resolve_subscriber_code_best_effort
@permission_classes([AllowAny])
@throttle_classes([LogIngestThrottle])
def ingest_log_view(request):
    if not _valid_api_key(request):
        return Response(
            {"success": False, "message": "API key inválida o no configurada."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    ser = LogEventIngestSerializer(data=request.data)
    if not ser.is_valid():
        return Response({"success": False, "errors": ser.errors}, status=status.HTTP_400_BAD_REQUEST)

    data = ser.validated_data
    subscriber_code = _resolve_subscriber_code_best_effort(request)

    record_log_event(
        platform=data["platform"],
        level=data.get("level") or "error",
        message=data["message"],
        stack=data.get("stack") or "",
        breadcrumbs=data.get("breadcrumbs"),
        extra=data.get("extra"),
        app_version=data.get("appVersion") or "",
        device_type=data.get("deviceType") or "",
        subscriber_code=subscriber_code,
        client_ip=get_client_ip(request),
    )

    return Response({"success": True}, status=status.HTTP_201_CREATED)
