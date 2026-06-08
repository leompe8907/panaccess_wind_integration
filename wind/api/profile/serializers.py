from django.contrib.auth import get_user_model
from rest_framework import serializers

from wind.models import ListOfProducts
from wind.serializers import ListOfProductsSerializer

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
