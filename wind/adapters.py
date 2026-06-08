import logging
import json

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.core.exceptions import MultipleObjectsReturned
from django.test import RequestFactory
from rest_framework.exceptions import ValidationError

from wind.models import SubscriberEmailRegistry, ListOfSubscriber
from wind.functions.create_subscriber import create_subscriber_view

logger = logging.getLogger(__name__)

def create_subscriber_in_panaccess(email, first_name, last_name, auto_generate_code=True, comment=""):
    """
    Llama a la vista de creación de suscriptor simulando un request.
    """
    factory = RequestFactory()
    data = {
        'lastName': last_name,
        'firstName': first_name,
        'email': email,
        'comment': comment
    }
    request = factory.post('/dummy/', data=json.dumps(data), content_type='application/json')
    request.wind_internal_create = True  # bypass throttling
    response = create_subscriber_view(request)
    return response.data

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
        user_email = sociallogin.user.email
        if not user_email:
            logger.error("El proveedor social no retornó un email")
            raise ValidationError("Se requiere un correo electrónico del proveedor social.")

        existing_local_user = get_user_model().objects.filter(email=user_email).first()
        if existing_local_user and sociallogin.user and sociallogin.user.pk != existing_local_user.pk:
            sociallogin.user = existing_local_user

        if sociallogin.is_existing:
            return

        logger.info(f"Procesando login social para email: {user_email}")

        # 1. Verificar si ya existe en PanAccess localmente (por correo)
        try:
            registry = SubscriberEmailRegistry.objects.get(email=user_email)
            subscriber_code = registry.subscriber_code
            logger.info(f"Usuario existente encontrado en registro: {subscriber_code}")
            return

        except SubscriberEmailRegistry.DoesNotExist:
            logger.info(
                f"Email no registrado en SubscriberEmailRegistry: {user_email}. "
                "Verificando si el suscriptor ya existe y, si es necesario, aprovisionando."
            )

            # Caso: ya existe el suscriptor en BD local (ListOfSubscriber) pero falta el registro de email.
            existing_subscriber = (
                ListOfSubscriber.objects.filter(emails__iexact=user_email).first()
            )
            if existing_subscriber and existing_subscriber.code:
                SubscriberEmailRegistry.objects.update_or_create(
                    email=user_email,
                    defaults={
                        'subscriber_code': existing_subscriber.code,
                        'has_purchased': False,
                    },
                )
                logger.info(
                    "SubscriberEmailRegistry creado/actualizado desde ListOfSubscriber: %s -> %s",
                    user_email,
                    existing_subscriber.code,
                )
                return
            
            # Extraer nombres de la cuenta social
            extra_data = sociallogin.account.extra_data
            first_name = extra_data.get('given_name', sociallogin.user.first_name)
            last_name = extra_data.get('family_name', sociallogin.user.last_name)
            
            if not last_name:
                last_name = "Social Login"
            if not first_name:
                first_name = user_email.split('@')[0]

            # 2. Crear el suscriptor en PanAccess
            try:
                result = create_subscriber_in_panaccess(
                    email=user_email,
                    first_name=first_name,
                    last_name=last_name,
                    auto_generate_code=True,
                    comment="Creado vía Google/Facebook Social Login"
                )
                
                if result.get('success'):
                    sub_code = result.get('subscriber_code')
                    logger.info(f"Suscriptor creado en PanAccess exitosamente: {sub_code}")
                else:
                    logger.error(f"Falla al crear suscriptor en PanAccess: {result.get('message')}")
                    raise ValidationError(f"No se pudo crear la cuenta en el sistema proveedor: {result.get('message')}")
                    
            except Exception as e:
                logger.error(f"Error crítico conectando con PanAccess durante Social Login: {str(e)}")
                raise ValidationError("Error al procesar el registro con PanAccess. Inténtalo más tarde.")

    def save_user(self, request, sociallogin, form=None):
        """
        Guarda el usuario local en Django. Sincroniza nombres.
        """
        user = super().save_user(request, sociallogin, form)
        
        extra_data = sociallogin.account.extra_data
        user.first_name = extra_data.get('given_name', user.first_name)
        user.last_name = extra_data.get('family_name', user.last_name)
        user.save()
        
        return user
