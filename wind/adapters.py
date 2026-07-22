import logging

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.core.exceptions import MultipleObjectsReturned
from rest_framework.exceptions import ValidationError

from wind.services.social_login_provisioning import (
    SocialLoginSubscriberNotFound,
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

    @staticmethod
    def _is_email_verified_by_provider(sociallogin, email: str) -> bool:
        """
        No confiar en el email del proveedor social sin chequear que él
        mismo lo marque como verificado -- si no, alguien podría "entrar"
        con un correo que no controla de verdad y quedar fusionado con la
        cuenta local existente de otra persona (account takeover).

        allauth ya normaliza esto en `sociallogin.email_addresses` (lista de
        EmailAddress con `.verified`, poblada según cada provider). Como
        respaldo se revisa también `extra_data.email_verified` (Google la
        expone ahí directamente).
        """
        for addr in getattr(sociallogin, "email_addresses", None) or []:
            if normalize_social_email(getattr(addr, "email", "")) == email:
                return bool(getattr(addr, "verified", False))

        extra_data = sociallogin.account.extra_data or {}
        if "email_verified" in extra_data:
            return bool(extra_data.get("email_verified"))

        # El proveedor no informa nada sobre verificación: no se asume que
        # está verificado.
        return False

    def pre_social_login(self, request, sociallogin):
        """
        Invocado tras el login social exitoso pero antes de iniciar sesión en Django.
        """
        user_email = normalize_social_email(sociallogin.user.email)
        if not user_email:
            logger.error("El proveedor social no retornó un email")
            raise ValidationError("Se requiere un correo electrónico del proveedor social.")

        sociallogin.user.email = user_email

        if not self._is_email_verified_by_provider(sociallogin, user_email):
            logger.warning(
                "Login social rechazado: el proveedor no confirma que %s esté verificado",
                user_email,
            )
            raise ValidationError(
                "Tu proveedor social no confirma que este correo esté verificado. "
                "Verifica tu correo con el proveedor e intenta de nuevo."
            )

        existing_local_user = get_user_model().objects.filter(email__iexact=user_email).first()
        if existing_local_user and sociallogin.user and sociallogin.user.pk != existing_local_user.pk:
            sociallogin.user = existing_local_user

        extra_data = sociallogin.account.extra_data or {}
        first_name = extra_data.get("given_name", sociallogin.user.first_name or "")
        last_name = extra_data.get("family_name", sociallogin.user.last_name or "")

        logger.info("Procesando login social para email: %s", user_email)

        # `sociallogin.account.provider` es el id de proveedor de allauth
        # ("google"/"facebook") -- este es el punto real donde se crea el
        # suscriptor nuevo la primera vez que alguien hace login social (antes
        # de que `GoogleLoginView`/`FacebookLoginView.get_response()` lleguen
        # a correr), así que si no se pasa el proveedor acá, todo suscriptor
        # nuevo -- venga de Google o Facebook -- cae al prefijo por defecto
        # "BG$" sin importar el proveedor real (encontrado en revisión
        # adversarial de este mismo ajuste).
        try:
            subscriber_code = ensure_subscriber_for_social_email(
                user_email,
                first_name=first_name,
                last_name=last_name,
                comment="Creado vía Google/Facebook Social Login",
                social_provider=sociallogin.account.provider,
            )
        except SocialLoginSubscriberNotFound:
            # FeatureConfig.SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER activo:
            # el cliente pidió que, mientras esté prendida, no se auto-registre
            # a quien haga login social sin tener ya un suscriptor -- se
            # devuelve un aviso específico en vez del error genérico de abajo.
            #
            # OJO (revisión adversarial): este `raise` ocurre dentro de
            # `Serializer.validate()` (vía `complete_social_login`), y DRF
            # normaliza cualquier ValidationError ahí con
            # `as_serializer_error()` -- si el `detail` es un dict, cada valor
            # queda envuelto en una lista (`{"error_type": ["SubscriberNotFound"]}`,
            # `"success"` se vuelve el string `"False"` dentro de una lista,
            # truthy en JS) en vez del JSON plano que uno esperaría. Por eso
            # se usa un string simple, igual que el resto de los `raise
            # ValidationError(...)` de este mismo método (ya probado en
            # producción) -- así el shape final es el estándar de DRF
            # (`{"non_field_errors": ["<mensaje>"]}`), sin sorpresas. El
            # prefijo "SubscriberNotFound: " deja un texto distinguible por
            # si el frontend quiere matchear sin depender de una clave nueva.
            logger.info(
                "Login social rechazado (SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER activo): "
                "no existe suscriptor para %s",
                user_email,
            )
            raise ValidationError(
                "SubscriberNotFound: No existe un suscriptor asociado a este correo."
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
