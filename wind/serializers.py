from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    ListOfSubscriber, SubscriberInfo, ListOfProducts, UDIDAuthRequest,
)

User = get_user_model()

class ListOfSubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListOfSubscriber
        fields = '__all__'
        
    def validate_code(self, value):
        if value:
            return value.strip()
        return value


class ListOfProductsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListOfProducts
        fields = '__all__'


class JWTUserDetailsSerializer(serializers.ModelSerializer):
    subscriber_code = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['pk', 'email', 'first_name', 'last_name', 'subscriber_code']
        read_only_fields = ['pk', 'email', 'first_name', 'last_name', 'subscriber_code']

    def get_subscriber_code(self, obj):
        # Antes solo miraba SubscriberEmailRegistry -- distinto (y más angosto)
        # que el resolver que ya usan el resto de los endpoints autenticados
        # (perfil, dispositivos vinculados, etc.), que además revisa
        # SubscriberLoginInfo por username numérico y ListOfSubscriber.emails
        # como respaldo. Un suscriptor real cuyo código solo resolviera por
        # esas otras vías recibía "subscriber_code": null en el login pese a
        # tener cuenta -- ahora usa el mismo resolver en todos lados.
        from wind.services.subscriber_catalog import resolve_subscriber_code_for_user

        if not obj:
            return None
        return resolve_subscriber_code_for_user(obj)


class CreateSubscriberSerializer(serializers.Serializer):
    lastName = serializers.CharField(required=True, max_length=100)
    firstName = serializers.CharField(required=True, max_length=100)
    email = serializers.EmailField(required=True, help_text="Email requerido para validación única")
    
    code = serializers.CharField(
        required=False, 
        allow_null=True, 
        allow_blank=True, 
        max_length=100,
        help_text="Código del suscriptor (documento)."
    )
    document_number = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    document_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    hcId = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    comment = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    # Antes se tomaban directo de `request.data` sin pasar por este
    # serializer (ver auditoría) -- se enviaban tal cual al payload de
    # PanAccess, sin límite de longitud ni formato. Ahora quedan validados
    # como el resto de los campos.
    countryCode = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=10,
        help_text="Código de país ISO (ej. DO). Por defecto DO si no se envía.",
    )
    regionId = serializers.IntegerField(required=False, allow_null=True)
    technicalNotes = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=500)
    caf = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)

    def validate_countryCode(self, value):
        if value and not value.strip().isalpha():
            raise serializers.ValidationError("countryCode debe ser un código de país (solo letras).")
        return value.strip().upper() if value else value


class ValidateSubscriberEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True, help_text="Email a validar contra registros existentes")


class UDIDAssociationSerializer(serializers.Serializer):
    udid = serializers.CharField(max_length=100)
    # Antes solo el `udid` (8 caracteres hex, ~4mil millones de combinaciones)
    # actuaba como credencial de todo el flujo de pareo. `temp_token` ya se
    # generaba en `UDIDAuthRequest.save()` pero no lo verificaba nadie (ver
    # auditoría) -- ahora es obligatorio y es el secreto real que demuestra
    # que quien llama de verdad tiene el pareo en curso (lo recibió junto al
    # QR), no solo que adivinó/enumeró un udid.
    temp_token = serializers.CharField(max_length=255)
    subscriber_code = serializers.CharField(max_length=100)
    sn = serializers.CharField(max_length=100)
    operator_id = serializers.CharField(max_length=100)
    method = serializers.ChoiceField(choices=[('automatic', 'Automatic'), ('manual', 'Manual')], default='automatic')

    def validate(self, attrs):
        import hmac

        udid = attrs['udid']
        subscriber_code = attrs['subscriber_code']
        sn = attrs['sn']

        try:
            udid_request = UDIDAuthRequest.objects.get(udid=udid)
        except UDIDAuthRequest.DoesNotExist:
            raise serializers.ValidationError("UDID no encontrado")

        # `attempts_count` cuenta acá -- el intento real de asociación --
        # y ya no en la consulta de estado (`GET /wind/validate/`, ver
        # `ValidateUDIDStatusView`). Antes se incrementaba en cada poll de
        # estado, así que una TV/app haciendo polling normal cada pocos
        # segundos podía agotar el cupo (`is_valid()`, `max_attempts=5`)
        # sin que nadie intentara nada indebido -- ahora cuenta cada POST
        # real a este endpoint (éxito o fallo, p.ej. `temp_token`
        # incorrecto), que es lo que de verdad hay que limitar (revisión
        # adversarial).
        udid_request.attempts_count += 1
        udid_request.save(update_fields=['attempts_count'])

        if not hmac.compare_digest(attrs['temp_token'] or "", udid_request.temp_token or ""):
            raise serializers.ValidationError("temp_token inválido para este UDID")

        # Verificar si expiró
        if udid_request.is_expired():
            udid_request.status = 'expired'
            udid_request.save(update_fields=['status'])
            raise serializers.ValidationError("UDID expirado")

        if udid_request.status != 'pending':
            raise serializers.ValidationError(f"UDID no está pendiente. Estado: {udid_request.status}")

        try:
            subscriber = SubscriberInfo.objects.get(sn=sn)
        except SubscriberInfo.DoesNotExist:
            raise serializers.ValidationError("Smartcard SN no encontrada en SubscriberInfo")

        if subscriber.subscriber_code != subscriber_code:
            raise serializers.ValidationError("Este SN no pertenece al subscriber_code indicado")

        if subscriber.is_locked():
            raise serializers.ValidationError("La cuenta del suscriptor está bloqueada")

        conflict_qs = UDIDAuthRequest.objects.filter(
            sn=sn,
            subscriber_code=subscriber_code,
            status__in=['validated', 'used']
        ).exclude(udid=udid)

        if conflict_qs.exists():
            raise serializers.ValidationError("Este SN ya está asociado a otro UDID activo")

        attrs['subscriber'] = subscriber
        attrs['udid_request'] = udid_request
        return attrs
