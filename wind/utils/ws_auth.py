"""
Autenticación de conexiones WebSocket vía JWT (Fase 3 -- `/ws/device/`).

Los consumers de Channels no pasan por el stack normal de DRF
(`authentication_classes`) -- hasta ahora nada en este proyecto valida un
JWT sobre una conexión WebSocket (el pareo de TV, `/ws/auth/`, usa
`temp_token` como credencial, no JWT). Este módulo es infraestructura
nueva para el registro de dispositivos: el cliente manda su JWT (el mismo
que ya recibe de cualquier login -- manual, Google o Facebook) como
parámetro de conexión, y acá se valida server-side antes de confiar en
nada que el cliente diga sobre a qué suscriptor pertenece.

Reutiliza `PasswordAwareJWTAuthentication` (no `JWTAuthentication` a
secas) para que un access token emitido antes de un cambio de contraseña
también quede invalidado acá, igual que en cualquier request REST normal
de la API -- mismo criterio de seguridad, sin una ruta paralela más débil.
"""
from __future__ import annotations

import logging

from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from wind.services.jwt_invalidation import PasswordAwareJWTAuthentication

logger = logging.getLogger(__name__)

_jwt_auth = PasswordAwareJWTAuthentication()


def resolve_user_from_jwt(raw_token: str):
    """
    Valida un access token JWT (string) y devuelve el `User` Django
    correspondiente, o `None` si es inválido/expiró/pertenece a un usuario
    inactivo/fue emitido antes de un cambio de contraseña.

    Nunca lanza -- cualquier problema de validación se traduce en `None`;
    el caller (el consumer) decide qué hacer, típicamente cerrar la
    conexión sin crear ningún `DeviceSession`.
    """
    if not raw_token:
        return None
    try:
        validated_token = _jwt_auth.get_validated_token(raw_token.encode("utf-8"))
        return _jwt_auth.get_user(validated_token)
    except (InvalidToken, TokenError) as e:
        logger.debug("WS JWT inválido para /ws/device/: %s", e)
        return None
    except Exception:
        logger.exception("Error inesperado validando JWT de WS en /ws/device/")
        return None
