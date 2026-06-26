"""
Utilidades para validar emails y documentos en el registro de suscriptores.
Previene la creación de múltiples cuentas duplicadas.
Permite re-registro (Opción B) tras cierre de cuenta, sin segundo trial.
"""
from wind.models import SubscriberEmailRegistry, SubscriberDocumentRegistry, ListOfSubscriber
import logging

logger = logging.getLogger(__name__)


def _allows_reregistration(registry) -> bool:
    """Opción B: permitir alta si compró o si cerró la cuenta previamente."""
    if registry.has_purchased:
        return True
    if registry.account_closed_at:
        return True
    return False


def _has_active_local_subscriber_for_email(email_lower: str) -> bool:
    return ListOfSubscriber.objects.filter(
        emails__iexact=email_lower,
    ).exclude(status=ListOfSubscriber.STATUS_CLOSED).exists()


def _has_active_local_subscriber_for_document(document_normalized: str) -> bool:
    return ListOfSubscriber.objects.filter(
        code=document_normalized,
    ).exclude(status=ListOfSubscriber.STATUS_CLOSED).exists()


def validate_email_for_registration(email):
    """
    Valida si un email puede ser usado para crear una nueva cuenta.
    """
    email_lower = email.lower().strip()

    try:
        registry = SubscriberEmailRegistry.objects.get(email=email_lower)

        if _allows_reregistration(registry):
            if _has_active_local_subscriber_for_email(email_lower):
                message = "Este email ya está en uso por una cuenta activa."
                logger.warning("Re-registro bloqueado: email %s con suscriptor activo local", email_lower)
                return False, message, registry
            logger.info(
                "Email %s elegible para re-registro (cuenta cerrada o has_purchased)",
                email_lower,
            )
            return True, "Email válido para re-registro", registry

        message = "Este email ya está registrado. No se pueden crear múltiples cuentas con el mismo email."
        logger.warning(f"Intento de registro duplicado con email {email_lower}")
        return False, message, registry

    except SubscriberEmailRegistry.DoesNotExist:
        logger.info(f"Email {email_lower} no registrado previamente, permitiendo registro")
        return True, "Email válido para registro", None


def validate_document_for_registration(document):
    """
    Valida si un documento puede ser usado para crear una nueva cuenta.
    """
    document_normalized = document.strip().upper() if document else None

    if not document_normalized:
        return False, "El documento es requerido", None

    try:
        registry = SubscriberDocumentRegistry.objects.get(document=document_normalized)

        if _allows_reregistration(registry):
            if _has_active_local_subscriber_for_document(document_normalized):
                message = "Este documento ya está en uso por una cuenta activa."
                logger.warning(
                    "Re-registro bloqueado: documento %s con suscriptor activo local",
                    document_normalized,
                )
                return False, message, registry
            logger.info(
                "Documento %s elegible para re-registro (cuenta cerrada o has_purchased)",
                document_normalized,
            )
            return True, "Documento válido para re-registro", registry

        message = "Este documento ya está registrado. No se pueden crear múltiples cuentas con el mismo documento."
        logger.warning(f"Intento de registro duplicado con documento {document_normalized}")
        return False, message, registry

    except SubscriberDocumentRegistry.DoesNotExist:
        logger.info(f"Documento {document_normalized} no registrado previamente, permitiendo registro")
        return True, "Documento válido para registro", None


def validate_email_and_document(email, document):
    """
    Valida tanto email como documento. Ambos deben pasar la validación.
    """
    email_valid, email_message, email_registry = validate_email_for_registration(email)
    if not email_valid:
        return False, email_message, email_registry, None

    document_valid, document_message, document_registry = validate_document_for_registration(document)
    if not document_valid:
        return False, document_message, email_registry, document_registry

    return True, "Email y documento válidos para registro", email_registry, document_registry
