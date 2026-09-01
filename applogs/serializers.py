from rest_framework import serializers

from applogs.models import LogIssue

_MAX_BREADCRUMBS = 100
_MAX_STACK_CHARS = 8000
_MAX_MESSAGE_CHARS = 2000
_MAX_EXTRA_BYTES = 20_000


class LogEventIngestSerializer(serializers.Serializer):
    """
    Body de `POST /api/v1/logs/` -- ver
    docs/GUIA_INTEGRACION_UNIFICADA.md (sección de logs de diagnóstico) para
    el contrato completo pensado para appVideo/iOS/Android.
    """

    platform = serializers.ChoiceField(choices=[c[0] for c in LogIssue.PLATFORM_CHOICES])
    level = serializers.ChoiceField(choices=[c[0] for c in LogIssue.LEVEL_CHOICES], default=LogIssue.LEVEL_ERROR)
    message = serializers.CharField(max_length=_MAX_MESSAGE_CHARS)
    stack = serializers.CharField(required=False, allow_blank=True, max_length=_MAX_STACK_CHARS)
    breadcrumbs = serializers.ListField(required=False, allow_null=True)
    extra = serializers.DictField(required=False, allow_null=True)
    appVersion = serializers.CharField(required=False, allow_blank=True, max_length=50)
    deviceType = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def validate_breadcrumbs(self, value):
        if value is None:
            return value
        if len(value) > _MAX_BREADCRUMBS:
            raise serializers.ValidationError(f"Máximo {_MAX_BREADCRUMBS} breadcrumbs.")
        for item in value:
            if not isinstance(item, dict):
                raise serializers.ValidationError("Cada breadcrumb debe ser un objeto.")
        return value

    def validate_extra(self, value):
        if value is None:
            return value
        import json

        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("No se pudo serializar 'extra'.") from exc
        if size > _MAX_EXTRA_BYTES:
            raise serializers.ValidationError(f"'extra' no puede superar ~{_MAX_EXTRA_BYTES} bytes.")
        return value
