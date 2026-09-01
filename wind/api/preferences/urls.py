from django.urls import path

from wind.api.preferences.views import preferences_view

urlpatterns = [
    path("", preferences_view, name="preferences"),
]
