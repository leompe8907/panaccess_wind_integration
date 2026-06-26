from datetime import timedelta
import secrets
import uuid
import hashlib

from django.db import models
from django.utils import timezone
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.db.models.signals import post_save

from wind.utils.encryption import encrypt_value, decrypt_value


# ============================================================================
# CACHÉ LOCAL DESDE PANACCESS
# ============================================================================

class ListOfSubscriber(models.Model):
    id = models.CharField(primary_key=True, blank=True, unique=True, max_length=100)
    code = models.CharField(max_length=100, blank=True, null=True, unique=True, db_index=True)
    lastName = models.CharField(max_length=100, null=True, blank=True)
    firstName = models.CharField(max_length=100, null=True, blank=True)
    smartcards = models.JSONField(null=True, blank=True, db_index=True)
    created = models.DateTimeField(null=True, blank=True)
    
    # Información extendida
    regionId = models.IntegerField(null=True, blank=True)
    countryCode = models.CharField(max_length=10, null=True, blank=True)
    caf = models.CharField(max_length=255, null=True, blank=True)
    supervisor = models.CharField(max_length=255, null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    
    # Contactos
    emails = models.EmailField(null=True, blank=True, db_index=True)
    phones = models.JSONField(null=True, blank=True)
    faxes = models.JSONField(null=True, blank=True)
    skypes = models.JSONField(null=True, blank=True)
    mobiles = models.JSONField(null=True, blank=True)
    custodians = models.JSONField(null=True, blank=True)
    
    # Direcciones
    address1 = models.JSONField(null=True, blank=True)
    address2 = models.JSONField(null=True, blank=True)
    address3 = models.JSONField(null=True, blank=True)
    addressCount = models.IntegerField(default=0, null=True, blank=True)
    
    # Información adicional
    newsletterAccepted = models.BooleanField(default=False, null=True, blank=True)
    firstOrderTime = models.DateTimeField(null=True, blank=True)
    lastExpiryTime = models.DateTimeField(null=True, blank=True)
    uniqueLogin = models.IntegerField(null=True, blank=True)
    tags = models.JSONField(null=True, blank=True)

    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    STATUS_PENDING_CLOSURE = "pending_closure"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_PENDING_CLOSURE, "Pending closure"),
    ]
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.TextField(null=True, blank=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['emails']),
            models.Index(fields=['smartcards']),
        ]

    def __str__(self):
        name_parts = [self.firstName, self.lastName]
        name = ' '.join(filter(None, name_parts))
        if name:
            return f"{name} ({self.code or self.id})"
        return f"Suscriptor {self.code or self.id}"


class ListOfSmartcards(models.Model):
    sn = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    subscriberCode = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    lastName = models.CharField(max_length=100, blank=True, null=True)
    firstName = models.CharField(max_length=100, blank=True, null=True)
    pin = models.CharField(max_length=100, null=True, blank=True)
    pairedBox = models.CharField(max_length=100, null=True, blank=True)
    products = models.JSONField(null=True, blank=True)
    casIds = models.CharField(max_length=255, null=True, blank=True)
    packages = models.JSONField(null=True, blank=True)
    packageNames = models.JSONField(null=True, blank=True)
    configId = models.CharField(max_length=100, null=True, blank=True)
    configProtected = models.BooleanField(default=False, null=True, blank=True)
    alias = models.CharField(max_length=100, null=True, blank=True)
    regionId = models.IntegerField(null=True, blank=True)
    regionName = models.CharField(max_length=100, null=True, blank=True)
    masterSn = models.CharField(max_length=100, null=True, blank=True)
    hcId = models.CharField(max_length=100, null=True, blank=True)
    lastActivation = models.DateTimeField(null=True, blank=True)
    lastContact = models.DateTimeField(null=True, blank=True)
    lastServiceListDownload = models.DateTimeField(null=True, blank=True)
    lastActivationIP = models.CharField(max_length=100, null=True, blank=True)
    firmwareVersion = models.CharField(max_length=100, null=True, blank=True)
    camlibVersion = models.CharField(max_length=100, null=True, blank=True)
    lastApiKeyId = models.CharField(max_length=100, null=True, blank=True)
    blacklisted = models.BooleanField(default=False, null=True, blank=True)
    disabled = models.BooleanField(default=False, null=True, blank=True)
    defect = models.BooleanField(default=False, null=True, blank=True)
    stbModel = models.CharField(max_length=100, null=True, blank=True)
    stbVendor = models.CharField(max_length=100, null=True, blank=True)
    stbChipset = models.CharField(max_length=100, null=True, blank=True)
    mac = models.CharField(max_length=100, null=True, blank=True)
    manufacturer = models.CharField(max_length=100, null=True, blank=True)
    model = models.CharField(max_length=100, null=True, blank=True)
    fingerprint = models.CharField(max_length=100, null=True, blank=True)
    hardware = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['subscriberCode']),
            models.Index(fields=['sn']),
        ]

    def __str__(self):
        name_parts = [self.firstName, self.lastName]
        name = ' '.join(filter(None, name_parts))
        if name:
            return f"{name} (SN: {self.sn or 'N/A'})"
        return f"Smartcard {self.sn or 'N/A'}"


class ListOfProducts(models.Model):
    productId = models.IntegerField(primary_key=True, unique=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    ordered = models.IntegerField(default=0)
    activeOrders = models.IntegerField(default=0)
    flexiblyOrdered = models.IntegerField(default=0)
    activeFlexibleOrders = models.IntegerField(default=0)
    deleted = models.BooleanField(default=False)
    description = models.TextField(null=True, blank=True)
    minRunTime = models.IntegerField(default=0)
    minRunTimeType = models.CharField(max_length=100, null=True, blank=True)
    allowFlexibleRuntime = models.BooleanField(default=False)
    hasOptionalPackages = models.BooleanField(default=False)
    packages = models.JSONField(null=True, blank=True)
    optionalPackages = models.JSONField(null=True, blank=True)
    catchupGroups = models.JSONField(null=True, blank=True)
    streams = models.JSONField(null=True, blank=True)
    vodLibraries = models.JSONField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name or 'Sin nombre'} (ID: {self.productId})"


# ============================================================================
# AUTENTICACIÓN Y SEGURIDAD
# ============================================================================

class SubscriberLoginInfo(models.Model):
    """
    Credenciales de acceso crudas (encriptadas simétricamente con Fernet).
    """
    subscriberCode = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    login1 = models.IntegerField(null=True, blank=True, db_index=True)
    login2 = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    additionalLogins = models.JSONField(null=True, blank=True)
    password_hash = models.CharField(max_length=255, null=True, blank=True)
    licenses = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"Login Info - Subscriber: {self.subscriberCode or 'N/A'}"
    
    def set_password(self, raw_password):
        if raw_password:
            self.password_hash = encrypt_value(raw_password)
        else:
            self.password_hash = None
    
    def get_password(self):
        return decrypt_value(self.password_hash) if self.password_hash else None
    
    def check_password(self, raw_password):
        if not self.password_hash or not raw_password:
            return False
        return self.get_password() == raw_password


class SubscriberInfo(models.Model):
    """
    Perfil consolidado y activo del suscriptor.
    """
    subscriber_code = models.CharField(max_length=100)

    # Datos de Smartcard asociados a su sesión
    sn = models.CharField(max_length=100, null=True, blank=True)
    pin_hash = models.CharField(max_length=255, null=True, blank=True)
    first_name = models.CharField(max_length=100, null=True, blank=True)
    last_name = models.CharField(max_length=100, null=True, blank=True)
    lastActivation = models.DateTimeField(null=True, blank=True)
    lastContact = models.DateTimeField(null=True, blank=True)
    lastServiceListDownload = models.DateTimeField(null=True, blank=True)
    lastActivationIP = models.CharField(max_length=100, null=True, blank=True)
    lastApiKeyId = models.CharField(max_length=100, null=True, blank=True)
    products = models.JSONField(null=True, blank=True)
    packages = models.JSONField(null=True, blank=True)
    packageNames = models.JSONField(null=True, blank=True)
    model = models.CharField(max_length=100, null=True, blank=True)

    # Identificación numérica y contraseñas
    login1 = models.IntegerField(null=True, blank=True)
    login2 = models.CharField(max_length=100, null=True, blank=True)
    password_hash = models.CharField(max_length=255, null=True, blank=True)

    # Estado de cuenta
    activated = models.BooleanField(default=False)
    activation_date = models.DateTimeField(null=True, blank=True)
    
    # Protección de fuerza bruta
    failed_login_attempts = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_login = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['subscriber_code']),
            models.Index(fields=['sn']),
            models.Index(fields=['activated']),
        ]

    def set_password(self, raw_password):
        if raw_password:
            self.password_hash = encrypt_value(raw_password)
        else:
            self.password_hash = None
    
    def get_password(self):
        return decrypt_value(self.password_hash) if self.password_hash else None
    
    def check_password(self, raw_password):
        if not self.password_hash or not raw_password:
            return False
        return self.get_password() == raw_password
    
    def set_pin(self, raw_pin):
        if raw_pin:
            self.pin_hash = encrypt_value(raw_pin)
        else:
            self.pin_hash = None
    
    def get_pin(self):
        return decrypt_value(self.pin_hash) if self.pin_hash else None
    
    def check_pin(self, raw_pin):
        if not self.pin_hash or not raw_pin:
            return False
        return self.get_pin() == raw_pin
    
    def is_locked(self):
        if not self.locked_until:
            return False
        return timezone.now() < self.locked_until
    
    def lock_account(self, minutes=30):
        self.locked_until = timezone.now() + timedelta(minutes=minutes)
        self.save(update_fields=['locked_until'])
    
    def unlock_account(self):
        self.locked_until = None
        self.failed_login_attempts = 0
        self.save(update_fields=['locked_until', 'failed_login_attempts'])
    
    def activate(self):
        self.activated = True
        self.activation_date = timezone.now()
        self.save(update_fields=['activated', 'activation_date'])

    def __str__(self):
        return f"{self.subscriber_code} - {self.first_name} {self.last_name}"


# ============================================================================
# REGISTROS DE UNICIDAD PARA PREVENCIÓN DE FRAUDE
# ============================================================================

class SubscriberEmailRegistry(models.Model):
    """
    Control de correos registrados para evitar múltiples registros.
    """
    email = models.EmailField(unique=True, db_index=True)
    subscriber_code = models.CharField(max_length=100, null=True, blank=True)
    document = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    has_purchased = models.BooleanField(default=False)
    purchased_at = models.DateTimeField(null=True, blank=True)
    trial_used = models.BooleanField(default=False, db_index=True)
    trial_granted_at = models.DateTimeField(null=True, blank=True)
    trial_expires_at = models.DateTimeField(null=True, blank=True)
    eligible_for_trial = models.BooleanField(default=True)
    account_closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_subscriber_code = models.CharField(max_length=100, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['document']),
            models.Index(fields=['trial_used']),
            models.Index(fields=['account_closed_at']),
        ]
    
    def __str__(self):
        return f"{self.email} -> {self.subscriber_code or 'N/A'}"


class SubscriberDocumentRegistry(models.Model):
    """
    Control de documentos de identidad registrados.
    """
    document = models.CharField(max_length=50, unique=True, db_index=True)
    subscriber_code = models.CharField(max_length=100, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    has_purchased = models.BooleanField(default=False)
    purchased_at = models.DateTimeField(null=True, blank=True)
    trial_used = models.BooleanField(default=False, db_index=True)
    trial_granted_at = models.DateTimeField(null=True, blank=True)
    trial_expires_at = models.DateTimeField(null=True, blank=True)
    eligible_for_trial = models.BooleanField(default=True)
    account_closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_subscriber_code = models.CharField(max_length=100, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['document']),
            models.Index(fields=['trial_used']),
            models.Index(fields=['account_closed_at']),
        ]
    
    def __str__(self):
        return f"{self.document} -> {self.subscriber_code or 'N/A'}"


class SubscriberClosureLog(models.Model):
    """Auditoría de cierres de cuenta (append-only)."""

    STATUS_COMPLETED = "completed"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_COMPLETED, "Completed"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_FAILED, "Failed"),
    ]

    subscriber_code = models.CharField(max_length=100, db_index=True)
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscriber_closures",
    )
    reason = models.TextField(blank=True, default="")
    dry_run = models.BooleanField(default=False)
    panaccess_result = models.JSONField(null=True, blank=True)
    local_result = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_COMPLETED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["subscriber_code", "created_at"]),
        ]

    def __str__(self):
        return f"Closure {self.subscriber_code} ({self.status})"


# ============================================================================
# COMPATIBILIDAD CON SMART TV (PAIRING & CRYPTO)
# ============================================================================

class AppCredentials(models.Model):
    """
    Llaves RSA asociadas por tipo de aplicación.
    La clave privada encripta/firma y la pública se embebe en la app del Smart TV.
    """
    APP_TYPES = [
        ('android_tv', 'Android TV'),
        ('samsung_tv', 'Samsung Tizen TV'),
        ('lg_tv', 'LG webOS TV'),
        ('set_top_box', 'Set Top Box'),
        ('mobile_app', 'Mobile Application'),
        ('web_player', 'Web Player'),
    ]

    app_type = models.CharField(max_length=50, choices=APP_TYPES)
    app_version = models.CharField(max_length=20, default='1.0', db_index=True)
    private_key_pem = models.TextField(help_text="Clave privada - NUNCA enviar al cliente")
    public_key_pem = models.TextField(help_text="Clave pública - se envía o embebe en la app")

    is_active = models.BooleanField(default=True)
    is_compromised = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    last_used = models.DateTimeField(null=True, blank=True)
    usage_count = models.IntegerField(default=0)
    key_fingerprint = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        unique_together = [['app_type', 'app_version']]
        indexes = [
            models.Index(fields=['app_type', 'app_version', 'is_active']),
            models.Index(fields=['is_active', 'expires_at']),
        ]
    
    def save(self, *args, **kwargs):
        if self.public_key_pem and not self.key_fingerprint:
            self.key_fingerprint = hashlib.sha256(
                self.public_key_pem.encode()
            ).hexdigest()[:16]
        super().save(*args, **kwargs)
    
    def is_expired(self):
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at
    
    def is_usable(self):
        return self.is_active and not self.is_compromised and not self.is_expired()
    
    def __str__(self):
        status = "✅" if self.is_usable() else "❌"
        return f"{status} {self.app_type} v{self.app_version}"


class UDIDAuthRequest(models.Model):
    """
    Solicitudes de emparejamiento (Pairing Code) para TVs.
    """
    STATUSES = [
        ('pending', 'Pending'),
        ('validated', 'Validated'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
        ('used', 'Used'),
    ]
    
    METHODS = [
        ('automatic', 'Automatic'),
        ('manual', 'Manual'),
    ]
    
    udid = models.CharField(max_length=100, unique=True, db_index=True)
    subscriber_code = models.CharField(max_length=100, db_index=True, null=True, blank=True)
    sn = models.CharField(max_length=100, null=True, blank=True)
    temp_token = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='pending')
    method = models.CharField(max_length=20, choices=METHODS, default='automatic')
    
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    validated_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(null=True, blank=True)
    
    validated_by_operator = models.CharField(max_length=100, null=True, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    attempts_count = models.IntegerField(default=0)
    device_fingerprint = models.CharField(max_length=255, null=True, blank=True)
    
    app_type = models.CharField(max_length=50, null=True, blank=True)
    app_version = models.CharField(max_length=20, null=True, blank=True)
    encrypted_response_sent = models.BooleanField(default=False)
    
    app_credentials_used = models.ForeignKey(AppCredentials, on_delete=models.SET_NULL, null=True, blank=True)
    encryption_successful = models.BooleanField(default=False)
    credentials_delivered = models.BooleanField(default=False)
    
    class Meta:
        indexes = [
            models.Index(fields=['udid', 'status']),
            models.Index(fields=['expires_at']),
            models.Index(fields=['subscriber_code']),
            models.Index(fields=['subscriber_code', 'sn']),
        ]
    
    def save(self, *args, **kwargs):
        if not self.expires_at:
            # Expiración por defecto: 5 minutos
            self.expires_at = timezone.now() + timedelta(minutes=5)
        if not self.udid:
            # Generar código de 8 caracteres hexadecimales
            self.udid = secrets.token_hex(4)
        if not self.temp_token:
            self.temp_token = secrets.token_urlsafe(32)
        
        # Si se valida o usa, detener la expiración
        if self.status in ['validated', 'used']:
            self.expires_at = timezone.now() + timedelta(days=3650)
            
        super().save(*args, **kwargs)
    
    def is_expired(self):
        if self.status in ['validated', 'used']:
            return False
        return timezone.now() > self.expires_at
    
    def mark_credentials_delivered(self, app_credentials):
        self.credentials_delivered = True
        self.encryption_successful = True
        self.app_credentials_used = app_credentials
        self.save(update_fields=['credentials_delivered', 'encryption_successful', 'app_credentials_used'])
        
        app_credentials.last_used = timezone.now()
        app_credentials.usage_count += 1
        app_credentials.save(update_fields=['last_used', 'usage_count'])
    
    def mark_as_used(self):
        self.status = 'used'
        self.used_at = timezone.now()
        self.save(update_fields=['status', 'used_at'])

    def is_valid(self):
        from django.conf import settings
        max_attempts = getattr(settings, 'UDID_MAX_ATTEMPTS', 5)
        return (
            self.status == 'pending' and
            not self.is_expired() and
            self.attempts_count < max_attempts
        )
    
    def get_expiration_info(self):
        if self.status in ['validated', 'used']:
            return {
                'expires': False,
                'status': self.status,
                'message': f'UDID {self.status} - expiration stopped'
            }
        else:
            time_left = self.expires_at - timezone.now()
            return {
                'expires': True,
                'expires_at': self.expires_at,
                'is_expired': self.is_expired(),
                'time_remaining': time_left if time_left.total_seconds() > 0 else None
            }

    def __str__(self):
        expiry_info = "∞" if self.status in ['validated', 'used'] else "⏰"
        return f"UDID Auth: {self.udid} - {self.status} {expiry_info}"


class EncryptedCredentialsLog(models.Model):
    """
    Registro histórico de credenciales cifradas enviadas a Smart TVs.
    """
    udid = models.CharField(max_length=100, db_index=True)
    subscriber_code = models.CharField(max_length=100, db_index=True)
    sn = models.CharField(max_length=100, null=True, blank=True)
    
    app_type = models.CharField(max_length=50)
    app_version = models.CharField(max_length=20)
    app_credentials_id = models.ForeignKey(AppCredentials, on_delete=models.CASCADE)
    
    encryption_algorithm = models.CharField(max_length=50, default="AES-256-CBC + RSA-OAEP")
    encrypted_data_hash = models.CharField(max_length=64)
    
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    delivered_successfully = models.BooleanField(default=False)
    
    class Meta:
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['subscriber_code', 'app_type']),
        ]


# ============================================================================
# AUDITORÍA DE EVENTOS
# ============================================================================

class AuthAuditLog(models.Model):
    ACTION_TYPES = [
        ('udid_generated', 'UDID Generated'),
        ('udid_validated', 'UDID Validated'),
        ('udid_used', 'UDID Used'),
        ('login_attempt', 'Login Attempt'),
        ('login_success', 'Login Success'),
        ('login_failed', 'Login Failed'),
        ('account_locked', 'Account Locked'),
        ('account_unlocked', 'Account Unlocked'),
    ]
    
    action_type = models.CharField(max_length=20, choices=ACTION_TYPES)
    subscriber_code = models.CharField(max_length=100, null=True, blank=True)
    udid = models.CharField(max_length=100, null=True, blank=True)
    operator_id = models.CharField(max_length=100, null=True, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    details = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['subscriber_code']),
            models.Index(fields=['action_type']),
        ]
    
    def __str__(self):
        return f"{self.action_type} - {self.subscriber_code} - {self.timestamp}"


class UserProfile(models.Model):
    """
    Perfil extendido para el usuario nativo de Django.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    operator_code = models.CharField(max_length=50)
    document_number = models.CharField(max_length=20, null=True, blank=True, unique=True)

    def __str__(self):
        return f"{self.user.username} - {self.operator_code}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
