"""
Permisos reutilizables para API operativa y perfil de usuario.
"""
import hmac

from rest_framework.permissions import BasePermission, IsAuthenticated


class HasCrmApiKey(BasePermission):
    """
    Autenticación M2M por API key compartida para la integración del bot de
    CRM del cliente (ver appConfig.CrmIntegrationConfig y auditoría).

    Exige el header `X-CRM-Api-Key` con el valor configurado en
    CRM_EMAIL_CHECK_API_KEY, comparado en tiempo constante
    (hmac.compare_digest, mismo patrón que views_health.py). Si la key no
    está configurada en el entorno, deniega siempre (fail-closed) -- así
    nunca queda accidentalmente abierto por olvido de configuración.
    """

    message = "No autorizado."

    def has_permission(self, request, view):
        from appConfig import CrmIntegrationConfig

        expected = CrmIntegrationConfig.EMAIL_CHECK_API_KEY
        if not expected:
            return False

        provided = request.META.get("HTTP_X_CRM_API_KEY", "")
        if not provided:
            return False

        return hmac.compare_digest(provided, expected)


class IsOwnerSubscriber(BasePermission):
    """
    El campo `code` del body debe coincidir con el subscriber_code
    vinculado al email del usuario autenticado.
    """

    message = "No puede operar sobre otro suscriptor."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        code = request.data.get("code")
        if not code:
            return False

        from wind.services.subscriber_catalog import resolve_subscriber_code_for_user

        subscriber_code = resolve_subscriber_code_for_user(request.user)
        if not subscriber_code:
            return False

        return subscriber_code == code
