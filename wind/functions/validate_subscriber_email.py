"""
Vista para validar si el email de un suscriptor ya existe / puede usarse en registro.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from wind.models import ListOfSubscriber
from wind.permissions import HasCrmApiKey
from wind.serializers import ValidateSubscriberEmailSerializer
from wind.throttles import RegisterThrottle
from wind.utils.email_validation import validate_email_for_registration

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([HasCrmApiKey])
@throttle_classes([RegisterThrottle])
def validate_subscriber_email_view(request):
    """
    Valida si un email ya está registrado como suscriptor.

    Uso M2M exclusivo del bot de CRM del cliente (ver auditoría) -- exige
    el header `X-CRM-Api-Key` (CrmIntegrationConfig / HasCrmApiKey). Antes
    era AllowAny y permitía a cualquiera enumerar emails registrados.

    Body: { "email": "usuario@ejemplo.com" }

    Respuesta:
      - exists: True si hay registro o suscriptor activo con ese email
      - available: True si el email puede usarse para crear una cuenta
    """
    serializer = ValidateSubscriberEmailSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                "success": False,
                "message": "Datos inválidos",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    email_normalized = serializer.validated_data["email"].lower().strip()
    logger.info("Validando existencia de email de suscriptor: %s", email_normalized)

    is_valid, validation_message, email_registry = validate_email_for_registration(
        email_normalized
    )

    has_active_local = ListOfSubscriber.objects.filter(
        emails__iexact=email_normalized,
    ).exclude(status=ListOfSubscriber.STATUS_CLOSED).exists()

    if has_active_local:
        is_valid = False
        validation_message = "Este email ya está registrado."

    exists = email_registry is not None or has_active_local
    available = is_valid

    return Response(
        {
            "success": True,
            "exists": exists,
            "available": available,
            "message": validation_message,
        },
        status=status.HTTP_200_OK,
    )
