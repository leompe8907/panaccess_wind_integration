from django.contrib import admin

from applogs.models import LogEvent, LogIssue


class LogEventInline(admin.TabularInline):
    model = LogEvent
    extra = 0
    fields = ("created_at", "subscriber_code", "device_type", "app_version", "client_ip")
    readonly_fields = fields
    can_delete = False
    show_change_link = True
    ordering = ("-created_at",)


@admin.register(LogIssue)
class LogIssueAdmin(admin.ModelAdmin):
    """
    Panel de consulta para desarrolladores -- ver
    docs/LOGS_DIAGNOSTICO_2026-09-01.md. Lista issues agrupados (no eventos
    sueltos) para que N ocurrencias del mismo error se vean como una fila,
    no como N filas sin relación.
    """

    list_display = ("platform", "level", "status", "message", "occurrence_count", "first_seen_at", "last_seen_at")
    list_filter = ("platform", "level", "status")
    search_fields = ("message", "fingerprint")
    ordering = ("-last_seen_at",)
    readonly_fields = ("fingerprint", "occurrence_count", "first_seen_at", "last_seen_at", "last_alerted_at")
    inlines = [LogEventInline]
    actions = ["mark_resolved", "mark_ignored", "mark_open"]

    @admin.action(description="Marcar como resuelto")
    def mark_resolved(self, request, queryset):
        queryset.update(status=LogIssue.STATUS_RESOLVED)

    @admin.action(description="Marcar como ignorado")
    def mark_ignored(self, request, queryset):
        queryset.update(status=LogIssue.STATUS_IGNORED)

    @admin.action(description="Reabrir")
    def mark_open(self, request, queryset):
        queryset.update(status=LogIssue.STATUS_OPEN)


@admin.register(LogEvent)
class LogEventAdmin(admin.ModelAdmin):
    list_display = ("issue", "subscriber_code", "device_type", "app_version", "client_ip", "created_at")
    list_filter = ("device_type",)
    search_fields = ("subscriber_code", "issue__message")
    ordering = ("-created_at",)
    readonly_fields = ("issue", "created_at")
