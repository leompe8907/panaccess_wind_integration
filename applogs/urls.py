from django.urls import path

from applogs.views import ingest_log_view

urlpatterns = [
    path("", ingest_log_view, name="log-ingest"),
]
