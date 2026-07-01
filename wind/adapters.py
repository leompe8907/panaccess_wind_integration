import logging

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.core.exceptions import MultipleObjectsReturned
from rest_framework.exceptions import ValidationError

from wind.services.social_login_provisioning import (
    ensure_subscriber_for_social_email,
    normalize_social_email,
)
from wind.services.subscriber_auth import mark_portal_email_verified

logger = logging.getLogger(__name__)


class PanAccessSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Adaptador personalizado para Allauth que intercepta el login social
    y sincroniza/registra al usuario en el sistema PanAccess.
    """

    def get_app(self, request, provider, client_id=None, **kwargs):
        """
        Evita MultipleObjectsReturned al elegir de forma determinista una SocialApp.
        """
        try:
            return super().get_app(request, provider, client_id=client_id, **kwargs)
        except MultipleObjectsReturned:
            qs = SocialApp.objects.filter(provider=provider)

            site = getattr(request, "site", None)
            if site is not None:
                qs = qs.filter(sites=site)

            if client_id:
                qs = qs.filter(client_id=client_id)

            app = qs.order_by("id").first()
            if not app:
                raise

            logger.warning(
                "Multiple SocialApp found for provider '%s'. Using SocialApp(id=%s, name=%s).",
                provider,
                app.id,
                app.name,
            )
            return app

    def pre_social_login(self, request, sociallogin):
        """
        Invocado tras el login social exitoso pero antes de iniciar sesión en Django.
        """
        user_email = normalize_social_email(sociallogin.user.email)
        if not user_email:
            logger.error("El proveedor social no retornó un email")
            raise ValidationError("Se requiere un correo electrónico del proveedor social.")

        sociallogin.user.email = user_email

        existing_local_user = get_user_model().objects.filter(email__iexact=user_email).first()
        if existing_local_user and sociallogin.user and sociallogin.user.pk != existing_local_user.pk:
            sociallogin.user = existing_local_user

        extra_data = sociallogin.account.extra_data or {}
        first_name = extra_data.get("given_name", sociallogin.user.first_name or "")
        last_name = extra_data.get("family_name", sociallogin.user.last_name or "")

        logger.info("Procesando login social para email: %s", user_email)

        subscriber_code = ensure_subscriber_for_social_email(
            user_email,
            first_name=first_name,
            last_name=last_name,
            comment="Creado vía Google/Facebook Social Login",
        )
        if not subscriber_code:
            raise ValidationError(
                "No se pudo obtener o crear el suscriptor en PanAccess para este email."
            )

    def save_user(self, request, sociallogin, form=None):
        """
        Guarda el usuario local en Django. Sincroniza nombres.
        """
        user = super().save_user(request, sociallogin, form)

        extra_data = sociallogin.account.extra_data or {}
        user.first_name = extra_data.get("given_name", user.first_name)
        user.last_name = extra_data.get("family_name", user.last_name)
        user.save()
        mark_portal_email_verified(user, user.email)

        return user
