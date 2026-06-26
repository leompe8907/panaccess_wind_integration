from django.urls import path

from wind.api.profile.views import (
    profile_me_view,
    profile_password_view,
    profile_products_view,
    profile_subscriber_view,
    profile_close_account_view,
)

urlpatterns = [
    path("me/", profile_me_view, name="profile-me"),
    path("password/", profile_password_view, name="profile-password"),
    path("products/", profile_products_view, name="profile-products"),
    path("subscriber/", profile_subscriber_view, name="profile-subscriber"),
    path("account/close/", profile_close_account_view, name="profile-close-account"),
]
