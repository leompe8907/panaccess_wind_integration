from django.contrib import admin

from telemetry.models import (
    TelemetryOttChannelName,
    TelemetryOttViewEvent,
    TelemetryChannelDailyAgg,
    TelemetryUserChannelDailyAgg,
    TelemetryIngestCursor,
)


@admin.register(TelemetryOttChannelName)
class TelemetryOttChannelNameAdmin(admin.ModelAdmin):
    list_display = ("channel_id", "name", "updated_at")
    search_fields = ("channel_id", "name")


@admin.register(TelemetryOttViewEvent)
class TelemetryOttViewEventAdmin(admin.ModelAdmin):
    list_display = ("record_id", "channel_id", "subscriber_code", "duration_seconds", "event_date")
    list_filter = ("event_date",)
    search_fields = ("record_id", "subscriber_code", "smartcard_id")
    date_hierarchy = "event_date"


@admin.register(TelemetryChannelDailyAgg)
class TelemetryChannelDailyAggAdmin(admin.ModelAdmin):
    list_display = ("day", "channel_id", "views", "unique_subscribers", "total_duration_seconds")
    list_filter = ("day",)


@admin.register(TelemetryUserChannelDailyAgg)
class TelemetryUserChannelDailyAggAdmin(admin.ModelAdmin):
    list_display = ("day", "subscriber_code", "channel_id", "views", "total_duration_seconds")
    list_filter = ("day",)
    search_fields = ("subscriber_code",)


@admin.register(TelemetryIngestCursor)
class TelemetryIngestCursorAdmin(admin.ModelAdmin):
    list_display = ("source", "last_record_id", "updated_at")
