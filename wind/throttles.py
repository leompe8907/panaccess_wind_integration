"""
Límites de tasa por tipo de endpoint.
"""
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class AnonBurstThrottle(AnonRateThrottle):
    scope = "anon"


class UserBurstThrottle(UserRateThrottle):
    scope = "user"


class ProfileThrottle(UserRateThrottle):
    scope = "profile"


class SyncAdminThrottle(UserRateThrottle):
    scope = "sync_admin"


class RegisterThrottle(AnonRateThrottle):
    """Registro público /wind/create-subscriber/ — límite bajo por IP."""

    scope = "register"

    def allow_request(self, request, view):
        if getattr(request, "wind_internal_create", False):
            return True
        return super().allow_request(request, view)
