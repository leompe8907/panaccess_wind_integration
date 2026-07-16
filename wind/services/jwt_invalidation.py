"""
Invalidación de JWT existentes tras cambio/reset de contraseña.

`SIMPLE_JWT` (`ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION`) solo
blacklistea un refresh token *después* de rotarlo -- no invalida nada de
forma proactiva cuando el usuario cambia su contraseña. Un access/refresh
token robado antes del cambio sigue funcionando hasta que expira por su
cuenta. Este módulo cierra ese hueco:

  1) `mark_password_changed(user)` -- se llama desde
     `wind.services.password_reset.sync_password_locally` (compartida por
     el flujo de "olvidé mi contraseña" y por "cambiar contraseña" desde el
     perfil). Actualiza `UserSecurityProfile.password_changed_at` y
     blacklistea de una vez todos los refresh tokens vigentes del usuario.
  2) `PasswordAwareJWTAuthentication` -- subclase de `JWTAuthentication`
     que además rechaza cualquier access token cuyo "iat" sea anterior al
     último cambio de contraseña. El blacklist de simplejwt no cubre esto
     porque solo actúa sobre refresh tokens ya rotados.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

logger = logging.getLogger(__name__)


def mark_password_changed(user) -> None:
    """Registra el momento del cambio y blacklistea refresh tokens vigentes."""
    if not user:
        return

    from wind.models import UserSecurityProfile

    UserSecurityProfile.objects.update_or_create(
        user=user,
        defaults={"password_changed_at": timezone.now()},
    )
    _blacklist_outstanding_refresh_tokens(user)


def _blacklist_outstanding_refresh_tokens(user) -> None:
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )
    except Exception:
        logger.warning(
            "token_blacklist no disponible; no se pudieron invalidar los "
            "refresh tokens de %s tras el cambio de contraseña",
            getattr(user, "email", user),
        )
        return

    for token in OutstandingToken.objects.filter(user=user):
        try:
            BlacklistedToken.objects.get_or_create(token=token)
        except Exception:
            logger.warning(
                "No se pudo blacklistear refresh token id=%s de %s",
                token.id,
                getattr(user, "email", user),
                exc_info=True,
            )


class PasswordAwareJWTAuthentication(JWTAuthentication):
    """
    Igual que `JWTAuthentication`, pero rechaza cualquier access token cuyo
    "iat" (issued-at) sea anterior al último cambio de contraseña del
    usuario (`UserSecurityProfile.password_changed_at`). Si el usuario nunca
    cambió su contraseña por estos flujos (no tiene `security_profile`), no
    se aplica ninguna restricción adicional.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)

        try:
            changed_at = user.security_profile.password_changed_at
        except Exception:
            return user

        iat = validated_token.get("iat")
        if iat is None or changed_at is None:
            return user

        issued_at = datetime.fromtimestamp(iat, tz=dt_timezone.utc)
        if issued_at < changed_at:
            raise InvalidToken(
                "Token emitido antes del último cambio de contraseña; inicia sesión de nuevo."
            )

        return user
