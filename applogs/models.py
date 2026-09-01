from django.db import models


class LogIssue(models.Model):
    """
    Un error agrupado (mismo `fingerprint`) -- puede tener muchas
    `LogEvent` (una por ocurrencia real). El panel de diagnóstico lista
    `LogIssue`, no eventos sueltos, para que 500 ocurrencias del mismo
    error de red no se vean como 500 filas sin relación entre sí.
    """

    PLATFORM_WEB = "web"
    PLATFORM_TV_TIZEN = "tv_tizen"
    PLATFORM_TV_WEBOS = "tv_webos"
    PLATFORM_IOS = "ios"
    PLATFORM_ANDROID = "android"
    PLATFORM_BACKEND = "backend"
    PLATFORM_CHOICES = [
        (PLATFORM_WEB, "Web"),
        (PLATFORM_TV_TIZEN, "TV Tizen"),
        (PLATFORM_TV_WEBOS, "TV webOS"),
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_ANDROID, "Android"),
        (PLATFORM_BACKEND, "Backend"),
    ]

    LEVEL_ERROR = "error"
    LEVEL_WARNING = "warning"
    LEVEL_INFO = "info"
    LEVEL_CHOICES = [
        (LEVEL_ERROR, "Error"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_INFO, "Info"),
    ]

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Abierto"),
        (STATUS_RESOLVED, "Resuelto"),
        (STATUS_IGNORED, "Ignorado"),
    ]

    fingerprint = models.CharField(max_length=64, unique=True, db_index=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=LEVEL_ERROR)
    message = models.CharField(max_length=500)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)

    occurrence_count = models.PositiveIntegerField(default=0)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    # Último momento en que se mandó una alerta por este issue (nuevo o pico
    # de ocurrencias) -- ver applogs.services.maybe_alert. Nula si nunca se
    # alertó (ej. alertas desactivadas, o el issue nunca cruzó el umbral).
    last_alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["platform", "status", "-last_seen_at"], name="applogs_issue_plat_status_idx"),
            models.Index(fields=["-last_seen_at"], name="applogs_issue_last_seen_idx"),
        ]

    def __str__(self):
        return f"[{self.platform}/{self.level}] {self.message[:80]}"


class LogEvent(models.Model):
    """Una ocurrencia individual de un `LogIssue`, con el contexto puntual de ese caso."""

    issue = models.ForeignKey(LogIssue, related_name="events", on_delete=models.CASCADE)

    # Nunca se confía en un `subscriber_code` mandado por el cliente para
    # identidad/autorización (ver wind.services.subscriber_identity) -- acá
    # es solo un dato de contexto para que soporte pueda filtrar, resuelto
    # del JWT si vino uno; puede quedar vacío (ej. crash antes de loguearse).
    subscriber_code = models.CharField(max_length=100, blank=True, db_index=True)
    device_type = models.CharField(max_length=50, blank=True)
    app_version = models.CharField(max_length=50, blank=True)

    stack = models.TextField(blank=True)
    breadcrumbs = models.JSONField(null=True, blank=True)
    extra = models.JSONField(null=True, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["issue", "-created_at"], name="applogs_evt_issue_created_idx"),
            models.Index(fields=["subscriber_code"], name="applogs_event_subscriber_idx"),
        ]

    def __str__(self):
        return f"Event #{self.pk} de issue #{self.issue_id}"
