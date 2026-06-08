"""
Serializers para login (PanAccess / Django) y login social (Google).
"""
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from dj_rest_auth.registration.serializers import SocialLoginSerializer
from dj_rest_auth.serializers import LoginSerializer as BaseLoginSerializer
from rest_framework import exceptions, serializers

from wind.services.subscriber_auth import authenticate_portal_user


class PanAccessLoginSerializer(BaseLoginSerializer):
    """
    Login con texto libre (login1, login2, código o email) + contraseña.
    """

    username = serializers.CharField(label=_("Usuario"), required=True, allow_blank=False)
    email = serializers.EmailField(required=False, allow_blank=True, write_only=True)

    def validate(self, attrs):
        username = (attrs.get("username") or "").strip()
        password = attrs.get("password")
        if not username or not password:
            raise exceptions.ValidationError(_("Debe incluir usuario y contraseña."))

        user = authenticate_portal_user(username, password)
        if not user:
            raise exceptions.ValidationError(_("No se pudo iniciar sesión con esas credenciales."))

        self.validate_auth_user_status(user)

        if "dj_rest_auth.registration" in settings.INSTALLED_APPS:
            self.validate_email_verification_status(user, email=user.email)

        attrs["user"] = user
        return attrs


class GoogleIdTokenSocialLoginSerializer(SocialLoginSerializer):
    """
    Para Google: si el cliente envía solo 'access_token' (el JWT de
    Google Identity Services), lo usamos también como 'id_token' para
    que complete_login use _decode_id_token y no _fetch_user_info.
    """

    def validate(self, attrs):
        request = self._get_request()
        view = self.context.get('view')
        if view and getattr(view, 'adapter_class', None):
            adapter = view.adapter_class(request)
            if (
                adapter.provider_id == 'google'
                and attrs.get('access_token')
                and not attrs.get('id_token')
            ):
                attrs = attrs.copy()
                attrs['id_token'] = attrs['access_token']
        return super().validate(attrs)
