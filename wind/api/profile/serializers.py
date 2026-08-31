from django.contrib.auth import get_user_model
from rest_framework import serializers

from wind.utils.password_policy import validate_password_policy

User = get_user_model()


class ProfilePasswordSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    # Obligatorio desde fase 2 del rollout de Alto #6 (ver
    # docs/PLAN_VERIFICACION_CONTRASENA_ACTUAL_2026-08-26.md y
    # docs/VERIFICACION_CONTRASENA_ACTUAL_FASE2_2026-08-28.md). Web,
    # Android e iOS ya lo mandan en producción -- si falta, se rechaza acá
    # mismo con 400 antes de llegar a la vista.
    oldPass = serializers.CharField(max_length=255, write_only=True, allow_blank=False)
    newPass = serializers.CharField(max_length=255, write_only=True)

    def validate_newPass(self, value):
        # Rechaza acá (400, con este campo puntual en ser.errors) lo que
        # sabemos de antemano que PanAccess va a rechazar -- evita el
        # round-trip y, sobre todo, evita que ese rechazo salga como 502
        # (ver wind/utils/password_policy.py y la vista que usa este
        # serializer para el código estable que se agrega en la respuesta).
        error = validate_password_policy(value)
        if error:
            raise serializers.ValidationError(error)
        return value


class ProfileCloseAccountSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    confirm = serializers.CharField(max_length=100)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    dry_run = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["code"].strip() != attrs["confirm"].strip():
            raise serializers.ValidationError(
                {"confirm": ["Debe coincidir con tu código de suscriptor para confirmar."]}
            )
        return attrs
