import logging

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.facebook.views import FacebookOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView

from wind.auth_serializers import (
    GoogleIdTokenSocialLoginSerializer,
    PanAccessSocialLoginSerializer,
)
from wind.services.social_login_provisioning import (
    SocialLoginSubscriberNotFound,
    resolve_panaccess_credentials_for_user,
    resolve_subscriber_code_for_social_user,
)
from wind.services.udid_auth_service import associate_udid_after_social_login
from wind.throttles import SocialLoginThrottle
from wind.utils.websocket_utils import get_client_ip

logger = logging.getLogger(__name__)


def _attach_panaccess_credentials(response, user, *, comment: str, social_provider: str | None = None):
    """Añade panaccess_credentials a la respuesta JWT del login social."""
    try:
        credentials = resolve_panaccess_credentials_for_user(
            user,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            comment=comment,
            social_provider=social_provider,
        )
    except SocialLoginSubscriberNotFound:
        # Caso borde defensivo (revisión adversarial): en el flujo normal
        # `pre_social_login` (wind/adapters.py) ya corta antes de llegar acá
        # si SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER está activo y el correo
        # no tiene suscriptor -- pero este resolver tiene su propio fallback
        # (por si el User Django ya existe sin subscriber_code resuelto), así
        # que igual puede llegar acá. No debe tumbar la respuesta del login
        # ya emitido (JWT válido); solo se deja sin credenciales.
        logger.warning(
            "No hay suscriptor para %s (pk=%s) y SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER "
            "está activo -- no se auto-registra",
            user.email,
            user.pk,
        )
        response.data["panaccess_credentials"] = None
        return

    if credentials:
        response.data["panaccess_credentials"] = credentials
        return

    logger.error(
        "No se pudieron resolver credenciales PanAccess para %s (pk=%s)",
        user.email,
        user.pk,
    )
    response.data["panaccess_credentials"] = None


def _maybe_authorize_tv_pairing(
    request, user, response, *, comment: str, social_provider: str | None = None
) -> bool:
    """
    Fase 2 -- pareo de TV vía login social (QR escaneado desde el celular).

    Si el POST trae `udid`+`temp_token` (el celular está autorizando una TV,
    no haciendo un login social "normal"), resuelve/crea el suscriptor
    (mismo flujo de siempre, incluida la prueba gratis si es nuevo) y asocia
    el pareo -- pero SIN devolver `panaccess_credentials` en la respuesta
    (decisión del cliente: "solo autorizar la TV" -- el password real de
    PanAccess nunca debe tocar el cliente que hizo el login social, solo
    viaja cifrado backend->TV, igual que en Fase 1).

    Devuelve True si el request pedía pareo (se haya logrado o no -- el
    resultado detallado queda en `response.data["udid_pairing"]`); False si
    no traía esos campos, para que el caller siga con el comportamiento de
    login social de siempre.
    """
    udid = str(request.data.get("udid") or "").strip()
    temp_token = str(request.data.get("temp_token") or "").strip()
    if not udid or not temp_token:
        return False

    # resolve_subscriber_code_for_social_user puede terminar creando un
    # suscriptor nuevo en PanAccess (prueba gratis) -- si esa llamada falla
    # (red, PanAccess caído, etc.), no debe dejar escapar la excepción: el
    # login social en sí (allauth/JWT) ya se completó antes de llegar acá,
    # así que un fallo solo en esta parte debe reportarse como
    # "no se pudo autorizar la TV", no como un 500 genérico que además
    # dejaría sin usar la respuesta JWT ya generada (revisión adversarial,
    # segunda auditoría).
    try:
        subscriber_code = resolve_subscriber_code_for_social_user(
            user,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            comment=comment,
            social_provider=social_provider,
        )
    except SocialLoginSubscriberNotFound:
        # SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER activo y este correo no
        # tiene suscriptor -- distinto de un fallo real, se reporta con su
        # propio código para que el celular no lo confunda con un problema
        # transitorio de PanAccess.
        logger.info(
            "Pareo TV rechazado (SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER activo): "
            "no existe suscriptor para %s (pk=%s)",
            user.email,
            user.pk,
        )
        response.data["udid_pairing"] = {
            "ok": False,
            "code": "subscriber_not_found",
            "error": "No existe un suscriptor asociado a este correo.",
        }
        return True
    except Exception:
        logger.exception(
            "Error resolviendo subscriber_code para pareo TV (%s, pk=%s)",
            user.email,
            user.pk,
        )
        subscriber_code = None

    if not subscriber_code:
        logger.error(
            "No se pudo resolver subscriber_code para pareo TV (%s, pk=%s)",
            user.email,
            user.pk,
        )
        response.data["udid_pairing"] = {
            "ok": False,
            "code": "subscriber_unresolved",
            "error": "No se pudo resolver/crear el suscriptor",
        }
        return True

    result = associate_udid_after_social_login(
        udid=udid,
        temp_token=temp_token,
        subscriber_code=subscriber_code,
        client_ip=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    # El detalle de excepción interna (si lo hay) solo va al log del
    # servidor, nunca al cliente (mismo patrón ya aplicado en Fase 1 a
    # AuthenticateWithUDIDView/DisassociateUDIDView/consumers.py).
    response.data["udid_pairing"] = {k: v for k, v in result.items() if k != "details"}
    return True


class GoogleLoginView(SocialLoginView):
    """
    Vista para procesar el login con Google mediante API REST.
    El cliente (frontend) envía el JWT de Google Identity en 'access_token';
    el serializer lo trata como id_token para que allauth decodifique el JWT.
    """
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    serializer_class = GoogleIdTokenSocialLoginSerializer
    throttle_classes = [SocialLoginThrottle]

    callback_url = 'http://localhost:8000/accounts/google/login/callback/'

    def get_response(self):
        response = super().get_response()
        if _maybe_authorize_tv_pairing(
            self.request, self.user, response,
            comment="Creado vía Google Social Login (pareo TV)",
            social_provider="google",
        ):
            # Camino "solo autorizar la TV" (Fase 2): a propósito NO se
            # llama a _attach_panaccess_credentials acá -- el password real
            # nunca debe llegar al celular en este flujo.
            response.data.setdefault("panaccess_credentials", None)
            return response
        _attach_panaccess_credentials(
            response,
            self.user,
            comment="Creado vía Google Social Login",
            social_provider="google",
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
    throttle_classes = [SocialLoginThrottle]

    def get_response(self):
        response = super().get_response()
        if _maybe_authorize_tv_pairing(
            self.request, self.user, response,
            comment="Creado vía Facebook Social Login (pareo TV)",
            social_provider="facebook",
        ):
            response.data.setdefault("panaccess_credentials", None)
            return response
        _attach_panaccess_credentials(
            response,
            self.user,
            comment="Creado vía Facebook Social Login",
            social_provider="facebook",
        )
        return response
