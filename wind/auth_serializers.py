"""
Serializers para login (PanAccess / Django) y login social (Google).
"""
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.db import IntegrityError
from django.http import HttpResponseBadRequest
from django.utils.translation import gettext_lazy as _
from allauth.account import app_settings as allauth_account_settings
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount
from dj_rest_auth.registration.serializers import SocialLoginSerializer
from dj_rest_auth.serializers import LoginSerializer as BaseLoginSerializer
from requests import HTTPError
from rest_framework import exceptions, serializers

from wind.services.subscriber_auth import authenticate_portal_user, mark_portal_email_verified


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


class PanAccessSocialLoginSerializer(SocialLoginSerializer):
    """
    Login social REST corregido para abonados PanAccess.

    dj-rest-auth usa connect=True y asume que login.account.user siempre existe.
    Si el User Django ya existe (p. ej. creado por login PanAccess) pero aún no
    tiene SocialAccount, allauth marca is_existing=True sin vincular la cuenta
    social → RelatedObjectDoesNotExist. Aquí conectamos o creamos la cuenta.
    """

    def _ensure_social_account(self, request, login):
        """Persiste SocialAccount y devuelve el User Django final."""
        user = login.user
        if login.account.pk and login.account.user_id:
            return login.account.user

        if user and user.pk:
            existing = SocialAccount.objects.filter(
                provider=login.account.provider,
                uid=login.account.uid,
            ).first()
            if existing:
                login.account = existing
                login.user = existing.user
                return existing.user
            login.connect(request, user)
            return login.user

        if allauth_account_settings.UNIQUE_EMAIL and user and user.email:
            existing_user = get_user_model().objects.filter(
                email__iexact=user.email,
            ).first()
            if existing_user:
                login.user = existing_user
                login.connect(request, existing_user)
                return existing_user

        if not login.is_existing:
            login.lookup()
            try:
                login.save(request, connect=False)
            except IntegrityError as ex:
                raise serializers.ValidationError(
                    _('User is already registered with this e-mail address.'),
                ) from ex
            self.post_signup(login, {})
            mark_portal_email_verified(login.user, login.user.email)

        if login.account.user_id:
            return login.account.user
        if login.user and login.user.pk:
            return login.user
        raise serializers.ValidationError(
            _('No se pudo completar el inicio de sesión social.'),
        )

    def validate(self, attrs):
        view = self.context.get('view')
        request = self._get_request()

        if not view:
            raise serializers.ValidationError(
                _('View is not defined, pass it as a context variable'),
            )

        logout(request)

        adapter_class = getattr(view, 'adapter_class', None)
        if not adapter_class:
            raise serializers.ValidationError(_('Define adapter_class in view'))

        adapter = adapter_class(request)
        app = adapter.get_provider().app

        access_token = attrs.get('access_token')
        code = attrs.get('code')
        id_token = attrs.get('id_token')

        if access_token:
            tokens_to_parse = {'access_token': access_token}
            if id_token:
                tokens_to_parse['id_token'] = id_token
        elif code:
            self.set_callback_url(view=view, adapter_class=adapter_class)
            self.client_class = getattr(view, 'client_class', None)
            if not self.client_class:
                raise serializers.ValidationError(_('Define client_class in view'))

            client = self.client_class(
                request,
                app.client_id,
                app.secret,
                adapter.access_token_method,
                adapter.access_token_url,
                self.callback_url,
                scope_delimiter=adapter.scope_delimiter,
                headers=adapter.headers,
                basic_auth=adapter.basic_auth,
            )
            from allauth.socialaccount.providers.oauth2.client import OAuth2Error

            try:
                token = client.get_access_token(code)
            except OAuth2Error as ex:
                raise serializers.ValidationError(
                    _('Failed to exchange code for access token'),
                ) from ex
            access_token = token['access_token']
            tokens_to_parse = {'access_token': access_token}
            id_token = token.get('id_token')
            for key in ['refresh_token', 'id_token', adapter.expires_in_key]:
                if key in token:
                    tokens_to_parse[key] = token[key]
        else:
            raise serializers.ValidationError(
                _('Incorrect input. access_token or code is required.'),
            )

        social_token = adapter.parse_token(tokens_to_parse)
        social_token.app = app

        try:
            if adapter.provider_id == 'google' and not code:
                login = self.get_social_login(
                    adapter,
                    app,
                    social_token,
                    response={'id_token': id_token or access_token},
                )
            else:
                login = self.get_social_login(adapter, app, social_token, access_token)
            ret = complete_social_login(request, login)
        except HTTPError:
            raise serializers.ValidationError(_('Incorrect value'))

        if isinstance(ret, HttpResponseBadRequest):
            raise serializers.ValidationError(ret.content)

        attrs['user'] = self._ensure_social_account(request, login)
        return attrs


class GoogleIdTokenSocialLoginSerializer(PanAccessSocialLoginSerializer):
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
