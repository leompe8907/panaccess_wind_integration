"""
Utilidades para generar códigos únicos de suscriptores.
"""
import logging
from wind.models import ListOfSubscriber
from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)


def generate_unique_subscriber_code(prefix='AUTO', max_attempts=10):
    """
    Genera un código único de suscriptor con formato AUTO + número secuencial.
    """
    # Antes: `order_by('-code').first()` ordenaba los códigos como texto,
    # no como número -- comparando texto, "AUTO9" queda "más grande" que
    # "AUTO10" (compara carácter por carácter, y en la segunda posición
    # "9" > "1"). Una vez que ya existen códigos de dos o más cifras, la
    # siguiente generación podía volver a partir de un número más bajo del
    # que realmente correspondía, desperdiciando intentos contra códigos
    # ya usados (revisión adversarial). Ahora se calcula el máximo
    # numérico real entre todos los códigos con este prefijo.
    existing_suffixes = ListOfSubscriber.objects.filter(
        code__startswith=prefix
    ).values_list('code', flat=True)

    last_number = 0
    for code in existing_suffixes:
        suffix = (code or '')[len(prefix):]
        if suffix.isdigit():
            last_number = max(last_number, int(suffix))

    next_number = last_number + 1
    
    # Intentar generar un código único
    for attempt in range(max_attempts):
        code = f"{prefix}{next_number}"
        
        # Verificar que no exista en la base de datos local
        if not ListOfSubscriber.objects.filter(code=code).exists():
            # Verificar que no exista en PanAccess (opcional pero recomendado)
            if not _code_exists_in_panaccess(code):
                logger.info(f"Código único generado: {code}")
                return code
            else:
                logger.warning(f"Código {code} existe en PanAccess, intentando siguiente...")
        
        # Si existe, intentar con el siguiente número
        next_number += 1
    
    raise Exception(f"No se pudo generar un código único después de {max_attempts} intentos")


def _code_exists_in_panaccess(code):
    """
    Verifica si un código de suscriptor existe en PanAccess.
    """
    try:
        panaccess = get_panaccess()
        try:
            response = panaccess.call('getSubscriber', {'code': code})
            
            if response.get('success'):
                return True
            else:
                return False
        except PanAccessException:
            return False
        except Exception:
            return False
            
    except Exception as e:
        logger.warning(f"Error verificando código en PanAccess: {str(e)}. Asumiendo que no existe.")
        return False


def validate_subscriber_code_uniqueness(code):
    """
    Valida que un código de suscriptor sea único.
    """
    if ListOfSubscriber.objects.filter(code=code).exists():
        return False
    
    if _code_exists_in_panaccess(code):
        return False
    
    return True
