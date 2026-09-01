"""
Preferencias de app sincronizadas entre dispositivos (control parental +
favoritos) -- ver docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md.

Punto clave de este módulo: la migración automática, una sola vez, de lo
guardado bajo `SubscriberPreferences.DEFAULT_PROFILE_KEY` hacia el primer
perfil real que una cuenta use, el día que esa cuenta activa perfiles de
PanAccess. Antes de eso, una cuenta sin perfiles solo tiene una fila
("default"); el día que crea su primer perfil real, esa fila se copia
hacia el nuevo `profile_key` en vez de que el usuario pierda lo que ya
había configurado. Cualquier perfil adicional que se cree después de ese
primero arranca vacío -- es el comportamiento esperado (cada perfil,
salvo el que "hereda" la migración, es independiente desde el vacío).
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from wind.models import SubscriberPreferences

logger = logging.getLogger(__name__)


def get_or_migrate_preferences(subscriber_code: str, profile_key: str) -> SubscriberPreferences:
    """
    Devuelve la fila de preferencias para `(subscriber_code, profile_key)`,
    creándola si hace falta. Si `profile_key` es un perfil real (no el
    sentinel "default") y esta cuenta todavía no tiene ningún otro perfil
    real registrado, copia lo que había bajo "default" hacia el nuevo
    perfil -- la migración automática documentada arriba.
    """
    try:
        return SubscriberPreferences.objects.get(
            subscriber_code=subscriber_code, profile_key=profile_key
        )
    except SubscriberPreferences.DoesNotExist:
        pass

    defaults = {}
    if profile_key != SubscriberPreferences.DEFAULT_PROFILE_KEY:
        other_real_profile_exists = (
            SubscriberPreferences.objects.filter(subscriber_code=subscriber_code)
            .exclude(profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY)
            .exists()
        )
        if not other_real_profile_exists:
            default_row = SubscriberPreferences.objects.filter(
                subscriber_code=subscriber_code,
                profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY,
            ).first()
            if default_row is not None:
                defaults = {
                    "parental": default_row.parental,
                    "favorite_channel_ids": default_row.favorite_channel_ids,
                }
                logger.info(
                    "Migrando preferencias 'default' -> perfil '%s' para %s "
                    "(primer perfil real de la cuenta)",
                    profile_key,
                    subscriber_code,
                )

    try:
        with transaction.atomic():
            return SubscriberPreferences.objects.create(
                subscriber_code=subscriber_code, profile_key=profile_key, **defaults
            )
    except IntegrityError:
        # Carrera: otro request (p. ej. otro dispositivo pidiendo lo mismo
        # casi al mismo tiempo) ya la creó entre el DoesNotExist de arriba
        # y este create -- la unique constraint lo protege, acá solo hay
        # que releerla.
        return SubscriberPreferences.objects.get(
            subscriber_code=subscriber_code, profile_key=profile_key
        )


def serialize_preferences(prefs: SubscriberPreferences) -> dict:
    return {
        "success": True,
        "profileKey": prefs.profile_key,
        "parental": prefs.parental,
        "favorites": prefs.favorite_channel_ids or [],
    }
