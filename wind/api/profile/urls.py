from django.urls import path

from wind.api.profile.views import (
    profile_me_view,
    profile_password_view,
    profile_password_otp_request_view,
    profile_password_otp_confirm_view,
    profile_products_view,
    profile_subscriber_view,
    profile_close_account_view,
    profile_request_account_deletion_view,
)

urlpatterns = [
    path("me/", profile_me_view, name="profile-me"),
    path("password/", profile_password_view, name="profile-password"),
    # Flujo nuevo (2026-09-14): cambiar contraseña con código OTP por
    # correo, en vez de `oldPass` -- coexiste con el endpoint de arriba,
    # no lo reemplaza (ver docs/CAMBIO_CONTRASENA_OTP_2026-09-14.md).
    path("password/otp/request-code/", profile_password_otp_request_view, name="profile-password-otp-request"),
    path("password/otp/confirm/", profile_password_otp_confirm_view, name="profile-password-otp-confirm"),
    path("products/", profile_products_view, name="profile-products"),
    path("subscriber/", profile_subscriber_view, name="profile-subscriber"),
    # Cierre inmediato -- sigue existiendo tal cual (uso interno/staff).
    path("account/close/", profile_close_account_view, name="profile-close-account"),
    # Nuevo flujo (2026-09-08): solicita eliminación, manda correo de
    # confirmación, no cierra nada todavía. El mismo endpoint sirve para
    # "reenviar correo" (llamarlo de nuevo con el mismo code).
    path(
        "account/close/request/",
        profile_request_account_deletion_view,
        name="profile-request-account-deletion",
    ),
]
