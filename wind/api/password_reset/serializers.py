from rest_framework import serializers


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    newPass = serializers.CharField(min_length=8, max_length=255, write_only=True)
    confirmPass = serializers.CharField(min_length=8, max_length=255, write_only=True)

    def validate(self, attrs):
        if attrs["newPass"] != attrs["confirmPass"]:
            raise serializers.ValidationError(
                {"confirmPass": "Las contraseñas no coinciden."}
            )
        return attrs
