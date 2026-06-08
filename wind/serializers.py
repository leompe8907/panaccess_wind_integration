from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
    ListOfSubscriber, ListOfSmartcards, SubscriberLoginInfo, SubscriberInfo,
    ListOfProducts, SubscriberEmailRegistry, SubscriberDocumentRegistry,
    UDIDAuthRequest, AuthAuditLog, AppCredentials
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


class ListOfSmartcardsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListOfSmartcards
        fields = '__all__'
        
    def validate_sn(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("El número de serie es requerido")
        return value.strip()


class SubscriberLoginInfoSerializer(serializers.ModelSerializer):
    password_hash = serializers.CharField(read_only=True)
    
    class Meta:
        model = SubscriberLoginInfo
        fields = '__all__'


class ListOfProductsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListOfProducts
        fields = '__all__'


class SubscriberInfoSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    
    password_hash = serializers.CharField(read_only=True)
    pin_hash = serializers.CharField(read_only=True)
    failed_login_attempts = serializers.IntegerField(read_only=True)
    locked_until = serializers.DateTimeField(read_only=True)
    
    class Meta:
        model = SubscriberInfo
        fields = [
            'id', 'subscriber_code', 'sn', 'first_name', 'last_name',
            'lastActivation', 'lastContact', 'lastServiceListDownload',
            'lastActivationIP', 'lastApiKeyId', 'products', 'packages',
            'packageNames', 'model', 'login1', 'login2', 'activated',
            'activation_date', 'last_login', 'created_at', 'updated_at',
            'password', 'pin',
            'password_hash', 'pin_hash', 'failed_login_attempts', 'locked_until'
        ]
        
    def create(self, validated_data):
        password = validated_data.pop('password', None)
        pin = validated_data.pop('pin', None)
        
        instance = SubscriberInfo.objects.create(**validated_data)
        
        if password:
            instance.set_password(password)
        if pin:
            instance.set_pin(pin)
            
        instance.save()
        return instance
        
    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        pin = validated_data.pop('pin', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
            
        if password:
            instance.set_password(password)
        if pin:
            instance.set_pin(pin)
            
        instance.save()
        return instance


class JWTUserDetailsSerializer(serializers.ModelSerializer):
    subscriber_code = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['pk', 'email', 'first_name', 'last_name', 'subscriber_code']
        read_only_fields = ['pk', 'email', 'first_name', 'last_name', 'subscriber_code']

    def get_subscriber_code(self, obj):
        if not obj or not getattr(obj, 'email', None):
            return None
        try:
            return SubscriberEmailRegistry.objects.get(email=obj.email).subscriber_code
        except SubscriberEmailRegistry.DoesNotExist:
            return None


class ContactSerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=['email', 'phone', 'fax', 'skype', 'mobile', 'custodian'],
        required=True
    )
    isBusiness = serializers.BooleanField(required=True)
    contact = serializers.CharField(required=True, max_length=255)


class AddressSerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=['private', 'company', 'bill', 'deliver'],
        required=True
    )
    name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    country = serializers.CharField(required=True, max_length=2)
    city = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    zip = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=20)
    street = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    addition = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    addition2 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    addition3 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    zone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    district = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    ownership = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=2)
    ownerName = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    ownerPhone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)


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
    
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    hcId = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    comment = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class UDIDAuthRequestSerializer(serializers.ModelSerializer):
    temp_token = serializers.CharField(read_only=True)
    attempts_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = UDIDAuthRequest
        fields = [
            'id', 'udid', 'subscriber_code', 'status', 'created_at',
            'expires_at', 'validated_at', 'used_at', 'validated_by_operator',
            'client_ip', 'user_agent', 'device_fingerprint',
            'temp_token', 'attempts_count'
        ]
        read_only_fields = ['udid', 'expires_at', 'created_at']
        
    def validate_subscriber_code(self, value):
        if not SubscriberInfo.objects.filter(subscriber_code=value).exists():
            raise serializers.ValidationError("Código de suscriptor no válido")
        return value


class AuthAuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthAuditLog
        fields = '__all__'
        read_only_fields = ['timestamp']


class UDIDAssociationSerializer(serializers.Serializer):
    udid = serializers.CharField(max_length=100)
    subscriber_code = serializers.CharField(max_length=100)
    sn = serializers.CharField(max_length=100)
    operator_id = serializers.CharField(max_length=100)
    method = serializers.ChoiceField(choices=[('automatic', 'Automatic'), ('manual', 'Manual')], default='automatic')

    def validate(self, attrs):
        udid = attrs['udid']
        subscriber_code = attrs['subscriber_code']
        sn = attrs['sn']

        try:
            udid_request = UDIDAuthRequest.objects.get(udid=udid)
        except UDIDAuthRequest.DoesNotExist:
            raise serializers.ValidationError("UDID no encontrado")

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
