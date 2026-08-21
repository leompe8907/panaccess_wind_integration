from django.urls import path

from telemetry.views import TopChannelsGlobalView

urlpatterns = [
    path("top-channels/", TopChannelsGlobalView.as_view(), name="telemetry_top_channels_global"),
]
