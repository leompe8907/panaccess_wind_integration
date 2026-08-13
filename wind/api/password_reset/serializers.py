from rest_framework import serializers

from wind.utils.password_policy import validate_password_policy


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    newPass = serializers.CharField(min_length=8, max_length=255, write_only=True)
    confirmPass = serializers.CharField(min_length=8, max_length=255, write_only=True)

    def validate_newPass(self, value):
        # Antes esta vista solo validaba longitud (min_length/max_length
        # arriba) -- ninguna regla de mayúscula/número/charset, a
        # diferencia de profile_password_view. Mismo motivo y misma
        # política que ahí (ver wind/utils/password_policy.py).
        error = validate_password_policy(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, attrs):
        if attrs["newPass"] != attrs["confirmPass"]:
            raise serializers.ValidationError(
                {"confirmPass": "Las contraseñas no coinciden."}
            )
        return attrs
