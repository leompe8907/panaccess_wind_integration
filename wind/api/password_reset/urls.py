from django.urls import path

from wind.api.password_reset.views import password_forgot_view, password_reset_confirm_view

urlpatterns = [
    path("forgot/", password_forgot_view, name="password_forgot"),
    path("reset-confirm/", password_reset_confirm_view, name="password_reset_confirm"),
]
