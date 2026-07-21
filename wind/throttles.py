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
    """
    Registro público /wind/create-subscriber/ — límite bajo por IP.

    Antes tenía un bypass vía `request.wind_internal_create` para que el
    aprovisionamiento de login social pudiera saltárselo sin pasar por el
    límite de tasa pensado para registro anónimo. Ese caso ahora invoca la
    lógica de creación directamente (`_create_subscriber_core`) sin pasar
    por esta vista/throttle en absoluto (ver
    wind.services.social_login_provisioning.create_subscriber_in_panaccess),
    así que ya no hace falta ningún atributo mágico para distinguirlo acá.
    """

    scope = "register"


class PasswordResetThrottle(AnonRateThrottle):
    """Recuperación de contraseña — límite bajo por IP."""

    scope = "password_reset"


class SocialLoginThrottle(AnonRateThrottle):
    """
    Login social (Google/Facebook) — antes sin throttle propio, caía en el
    límite genérico anónimo global (60/minute), mucho más permisivo que el
    resto de las acciones de auth. Este endpoint valida un token externo y
    dispara aprovisionamiento en PanAccess, así que conviene un límite
    dedicado (ver auditoría).
    """

    scope = "social_login"


class DeviceSessionThrottle(UserRateThrottle):
    """
    Listar/revocar dispositivos vinculados (Fase 3) — antes sin throttle
    propio, caía en el límite genérico de usuario (`UserBurstThrottle`,
    600/minute), pensado para navegación normal de la app, no para una
    acción de escritura que además dispara un broadcast por WebSocket
    (`notify_device_revoked`) por cada llamada. Un JWT válido pero
    comprometido podría, si no fuera por esto, golpear el endpoint de
    revocar cientos de veces por minuto sin ningún límite más ajustado
    (segunda auditoría).
    """

    scope = "device_session"
