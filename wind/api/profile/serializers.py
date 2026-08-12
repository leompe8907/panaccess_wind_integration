from django.contrib.auth import get_user_model
from rest_framework import serializers

from wind.models import ListOfProducts
from wind.serializers import ListOfProductsSerializer
from wind.utils.password_policy import validate_password_policy

User = get_user_model()


class ProfileMeSerializer(serializers.ModelSerializer):
    subscriber_code = serializers.SerializerMethodField()
    subscriber = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "pk",
            "email",
            "first_name",
            "last_name",
            "subscriber_code",
            "subscriber",
        ]
        read_only_fields = fields

    def get_subscriber_code(self, obj):
        from wind.services.subscriber_catalog import resolve_subscriber_code_for_user
        return resolve_subscriber_code_for_user(obj)

    def get_subscriber(self, obj):
        from wind.services.subscriber_catalog import build_subscriber_detail_payload
        code = self.get_subscriber_code(obj)
        if not code:
            return None
        try:
            return build_subscriber_detail_payload(code, refresh_if_missing=True)
        except Exception:
            return None


class ProfilePasswordSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
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


class ProfileProductSerializer(ListOfProductsSerializer):
    """Catálogo local sincronizado (lectura)."""

    class Meta(ListOfProductsSerializer.Meta):
        model = ListOfProducts
        fields = [
            "productId",
            "name",
            "description",
            "deleted",
            "packages",
            "optionalPackages",
            "updated_at",
        ]
