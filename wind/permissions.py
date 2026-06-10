"""
Permisos reutilizables para API operativa y perfil de usuario.
"""
from rest_framework.permissions import BasePermission, IsAuthenticated


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
