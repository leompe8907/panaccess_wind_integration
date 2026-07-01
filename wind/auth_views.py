import logging

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.facebook.views import FacebookOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView

from wind.auth_serializers import (
    GoogleIdTokenSocialLoginSerializer,
    PanAccessSocialLoginSerializer,
)
from wind.functions.getSubscriberLoginInfo import CallGetSubscriberLoginInfo
from wind.models import SubscriberEmailRegistry

logger = logging.getLogger(__name__)


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
        """
        Sobrescribimos la respuesta por defecto para también incluir las 
        credenciales nativas de PanAccess (login1, password).
        De esta manera, la app frontend puede tomar estas credenciales y
        solicitar el session_id directamente a PanAccess de forma autónoma.
        """
        response = super().get_response()
        user = self.user
        
        try:
            # Buscar el registro de correo para obtener el subscriber_code
            registry = SubscriberEmailRegistry.objects.get(email=user.email)
            subscriber_code = registry.subscriber_code
            
            # Llamar directamente a PanAccess para obtener login1 y password original
            login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)
            
            panaccess_credentials = {
                'login1': login_info.get('login1'),
                'password': login_info.get('password'),
                'login2': login_info.get('login2', ''),
                'subscriberCode': subscriber_code
            }
            response.data['panaccess_credentials'] = panaccess_credentials
            
        except SubscriberEmailRegistry.DoesNotExist:
            # Si por alguna razón no existe el registro, intentamos crear el suscriptor
            # usando el mismo flujo del endpoint create-subscriber/.
            try:
                logger.warning(
                    "No se encontró SubscriberEmailRegistry para %s. "
                    "Intentando crear suscriptor automáticamente.",
                    user.email,
                )
                from wind.adapters import create_subscriber_in_panaccess

                result = create_subscriber_in_panaccess(
                    email=user.email,
                    first_name=user.first_name or (user.email.split("@")[0] if user.email else ""),
                    last_name=user.last_name or "Social Login",
                    auto_generate_code=True,
                    comment="Creado vía Google Social Login (auto-recovery)",
                )

                # Reintentar la lectura del registro tras la creación
                registry = SubscriberEmailRegistry.objects.get(email=user.email)
                subscriber_code = registry.subscriber_code
                login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)

                response.data['panaccess_credentials'] = {
                    'login1': login_info.get('login1'),
                    'password': login_info.get('password'),
                    'login2': login_info.get('login2', ''),
                    'subscriberCode': subscriber_code,
                }

                # Adjuntar información del flujo de creación si existe
                response.data['subscriber_provisioning'] = {
                    'create_subscriber_result': result,
                }
            except Exception as e:
                logger.error(
                    "No se pudieron obtener/crear credenciales PanAccess para %s: %s",
                    user.email,
                    str(e),
                    exc_info=True,
                )
                response.data['panaccess_credentials'] = None
        except Exception as e:
            logger.error(f"Error obteniendo credenciales de PanAccess para la vista: {str(e)}")
            response.data['panaccess_credentials'] = None

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
        user = self.user

        try:
            registry = SubscriberEmailRegistry.objects.get(email=user.email)
            subscriber_code = registry.subscriber_code

            login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)
            panaccess_credentials = {
                'login1': login_info.get('login1'),
                'password': login_info.get('password'),
                'login2': login_info.get('login2', ''),
                'subscriberCode': subscriber_code,
            }
            response.data['panaccess_credentials'] = panaccess_credentials

        except SubscriberEmailRegistry.DoesNotExist:
            # En caso edge: el usuario existe pero falta la asociacion local.
            # Reintentamos el provisionamiento con create-subscriber.
            try:
                logger.warning(
                    "No se encontró SubscriberEmailRegistry para %s. "
                    "Intentando crear suscriptor automáticamente.",
                    user.email,
                )
                from wind.adapters import create_subscriber_in_panaccess

                result = create_subscriber_in_panaccess(
                    email=user.email,
                    first_name=user.first_name or (user.email.split('@')[0] if user.email else ""),
                    last_name=user.last_name or "Social Login",
                    auto_generate_code=True,
                    comment="Creado vía Facebook Social Login (auto-recovery)",
                )

                registry = SubscriberEmailRegistry.objects.get(email=user.email)
                subscriber_code = registry.subscriber_code
                login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)

                response.data['panaccess_credentials'] = {
                    'login1': login_info.get('login1'),
                    'password': login_info.get('password'),
                    'login2': login_info.get('login2', ''),
                    'subscriberCode': subscriber_code,
                }

                response.data['subscriber_provisioning'] = {
                    'create_subscriber_result': result,
                }
            except Exception:
                response.data['panaccess_credentials'] = None

        except Exception as e:
            logger.error(
                "Error obteniendo credenciales de PanAccess para Facebook: %s",
                str(e),
                exc_info=True,
            )
            response.data['panaccess_credentials'] = None

        return response
