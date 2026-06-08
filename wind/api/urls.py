from django.urls import include, path

from wind.api.task_views import task_status_view

urlpatterns = [
    path("profile/", include("wind.api.profile.urls")),
    path("tasks/<str:task_id>/", task_status_view, name="task-status"),
]
