import json

from rest_framework import serializers

from wind.models import SubscriberPreferences

# Guardarraíl defensivo, no un límite de negocio -- evita que un cliente
# roto (o malicioso) guarde un blob arbitrariamente grande en `parental`.
# El shape real (PIN hasheado + salt + IDs de canal bloqueados) pesa unos
# pocos cientos de bytes; 20 KB da margen de sobra sin abrir la puerta a
# abuso.
_MAX_PARENTAL_BLOB_BYTES = 20_000
_MAX_FAVORITES = 500


class SubscriberPreferencesUpdateSerializer(serializers.Serializer):
    profileKey = serializers.CharField(max_length=100, required=False, allow_blank=True)
    # Blob opaco -- el backend no interpreta su forma interna, solo lo
    # persiste tal cual (ver wind/services/subscriber_preferences.py).
    parental = serializers.JSONField(required=False, allow_null=True)
    # IDs de canal como texto -- coincide con cómo appVideo ya los compara
    # (`String(entry.channel_id)` en mostWatchedChannelsService.js), evita
    # sorpresas de tipo entre número/string según el origen del dato.
    favorites = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        allow_null=True,
        max_length=_MAX_FAVORITES,
    )

    def validate_profileKey(self, value):
        value = (value or "").strip()
        return value or SubscriberPreferences.DEFAULT_PROFILE_KEY

    def validate_parental(self, value):
        if value is None:
            return value
        if not isinstance(value, dict):
            raise serializers.ValidationError("Debe ser un objeto.")
        if len(json.dumps(value)) > _MAX_PARENTAL_BLOB_BYTES:
            raise serializers.ValidationError("Configuración de control parental demasiado grande.")
        return value
