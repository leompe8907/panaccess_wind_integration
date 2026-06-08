"""
Utilidades para validar emails y documentos en el registro de suscriptores.
Previene la creación de múltiples cuentas duplicadas.
"""
from wind.models import SubscriberEmailRegistry, SubscriberDocumentRegistry
import logging

logger = logging.getLogger(__name__)


def validate_email_for_registration(email):
    """
    Valida si un email puede ser usado para crear una nueva cuenta.
    """
    email_lower = email.lower().strip()
    
    try:
        registry = SubscriberEmailRegistry.objects.get(email=email_lower)
        
        # Si compró contenido, puede crear otra cuenta
        if registry.has_purchased:
            logger.info(f"Email {email_lower} ya registrado pero compró contenido, permitiendo nueva cuenta")
            return True, "Email registrado pero usuario compró contenido", registry
        
        # Si no compró, no puede crear otra cuenta
        message = "Este email ya está registrado. No se pueden crear múltiples cuentas con el mismo email."
        logger.warning(f"Intento de registro duplicado con email {email_lower}")
        return False, message, registry
            
    except SubscriberEmailRegistry.DoesNotExist:
        # Email nunca registrado, puede crear cuenta
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
        
        # Si compró contenido, puede crear otra cuenta
        if registry.has_purchased:
            logger.info(f"Documento {document_normalized} ya registrado pero compró contenido, permitiendo nueva cuenta")
            return True, "Documento registrado pero usuario compró contenido", registry
        
        # Si no compró, no puede crear otra cuenta
        message = "Este documento ya está registrado. No se pueden crear múltiples cuentas con el mismo documento."
        logger.warning(f"Intento de registro duplicado con documento {document_normalized}")
        return False, message, registry
            
    except SubscriberDocumentRegistry.DoesNotExist:
        # Documento nunca registrado, puede crear cuenta
        logger.info(f"Documento {document_normalized} no registrado previamente, permitiendo registro")
        return True, "Documento válido para registro", None


def validate_email_and_document(email, document):
    """
    Valida tanto email como documento. Ambos deben pasar la validación.
    """
    # Validar email
    email_valid, email_message, email_registry = validate_email_for_registration(email)
    if not email_valid:
        return False, email_message, email_registry, None
    
    # Validar documento
    document_valid, document_message, document_registry = validate_document_for_registration(document)
    if not document_valid:
        return False, document_message, email_registry, document_registry
    
    # Ambos son válidos
    return True, "Email y documento válidos para registro", email_registry, document_registry
