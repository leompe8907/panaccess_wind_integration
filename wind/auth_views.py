import logging

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.facebook.views import FacebookOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView

from wind.auth_serializers import (
    GoogleIdTokenSocialLoginSerializer,
    PanAccessSocialLoginSerializer,
)
from wind.services.social_login_provisioning import resolve_panaccess_credentials_for_user

logger = logging.getLogger(__name__)


def _attach_panaccess_credentials(response, user, *, comment: str):
    """Añade panaccess_credentials a la respuesta JWT del login social."""
    credentials = resolve_panaccess_credentials_for_user(
        user,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        comment=comment,
    )
    if credentials:
        response.data["panaccess_credentials"] = credentials
        return

    logger.error(
        "No se pudieron resolver credenciales PanAccess para %s (pk=%s)",
        user.email,
        user.pk,
    )
    response.data["panaccess_credentials"] = None


class GoogleLoginView(SocialLoginView):
    """
    Vista para procesar el login con Google mediante API REST.
    El cliente (frontend) envía el JWT de Google Identity en 'access_token';
    el serializer lo trata como id_token para que allauth decodifique el JWT.
    """
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    serializer_class = GoogleIdTokenSocialLoginSerializer

    callback_url = 'http://localhost:8000/accounts/google/login/callback/'

    def get_response(self):
        response = super().get_response()
        _attach_panaccess_credentials(
            response,
            self.user,
            comment="Creado vía Google Social Login",
        )
        return response


class FacebookLoginView(SocialLoginView):
    """
    Vista para procesar el login social con Facebook mediante API REST.

    El cliente (frontend) debe enviar un POST con:
      { "access_token": "<FACEBOOK_ACCESS_TOKEN>" }

    La respuesta incluye:
      - access/refresh JWT de Django
      - panaccess_credentials (login1/password/login2/subscriberCode)
    """

    adapter_class = FacebookOAuth2Adapter
    client_class = OAuth2Client
    serializer_class = PanAccessSocialLoginSerializer

    def get_response(self):
        response = super().get_response()
        _attach_panaccess_credentials(
            response,
            self.user,
            comment="Creado vía Facebook Social Login",
        )
        return response
