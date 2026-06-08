"""
Funciones de autenticación con PanAccess.
"""
import hashlib
import logging
import requests
from urllib.parse import urlencode

from appConfig import PanaccessConfig
from wind.exceptions import (
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError
)

logger = logging.getLogger(__name__)


def hash_password(password: str, salt: str = None) -> str:
    if salt is None:
        salt = PanaccessConfig.SALT
    
    return hashlib.md5((password + salt).encode()).hexdigest()


def login() -> str:
    """
    Realiza login en PanAccess y retorna el sessionId.
    """
    PanaccessConfig.validate()
    
    username = PanaccessConfig.USERNAME
    password = PanaccessConfig.PASSWORD
    api_token = PanaccessConfig.API_TOKEN
    base_url = PanaccessConfig.PANACCESS
    
    if not username or not password or not api_token:
        raise PanAccessAuthenticationError(
            "Faltan credenciales de PanAccess en la configuración. "
            "Verifica las variables de entorno: username, password, api_token"
        )
    
    # Hashear contraseña
    hashed_password = hash_password(password)
    
    # Preparar payload
    payload = {
        "username": username,
        "password": hashed_password,
        "apiToken": api_token
    }
    
    # URL del endpoint
    url = f"{base_url}?f=login&requestMode=function"
    
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    param_string = urlencode(payload)
    
    logger.info("Iniciando login")
    
    try:
        response = requests.post(
            url,
            data=param_string,
            headers=headers,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"Status code inesperado: {response.status_code}")
            raise PanAccessAPIError(
                f"Respuesta inesperada del servidor PanAccess: {response.status_code}",
                status_code=response.status_code
            )
        
        try:
            json_response = response.json()
        except ValueError as e:
            logger.error(f"Error parseando JSON: {str(e)}")
            raise PanAccessAPIError(
                f"Respuesta inválida del servidor PanAccess",
                status_code=response.status_code
            )
        
        success = json_response.get("success")
        
        if not success:
            error_message = json_response.get("errorMessage", "Login fallido sin mensaje explícito")
            answer = json_response.get("answer")
            logger.error(f"Login fallido: {error_message}")
            
            if answer == "false" or error_message:
                raise PanAccessAuthenticationError(
                    f"Error de autenticación: {error_message}"
                )
            
            raise PanAccessAPIError(
                f"Error en la respuesta de PanAccess: {error_message}",
                status_code=response.status_code
            )
        
        session_id = json_response.get("answer")
        
        if not session_id:
            logger.error("No se recibió sessionId en la respuesta")
            raise PanAccessAPIError(
                "Login exitoso pero no se recibió sessionId en la respuesta"
            )
        
        logger.info(f"Login exitoso - SessionId obtenido ({len(session_id)} caracteres)")
        return session_id
        
    except requests.exceptions.Timeout:
        logger.error("Timeout al intentar login (30s)")
        raise PanAccessTimeoutError(
            "Timeout al intentar conectarse con PanAccess. "
            "El servidor no respondió en 30 segundos."
        )
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Error conexión: {str(e)}")
        raise PanAccessConnectionError(
            f"Error de conexión con PanAccess: {str(e)}"
        )
    except (PanAccessAuthenticationError, PanAccessAPIError, PanAccessTimeoutError, PanAccessConnectionError):
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}", exc_info=True)
        raise PanAccessAPIError(
            f"Error inesperado al intentar login con PanAccess: {str(e)}"
        )


def logged_in(session_id: str) -> bool:
    """
    Verifica si un sessionId de PanAccess sigue siendo válido.
    """
    PanaccessConfig.validate()
    
    if not session_id:
        logger.debug("🔍 [logged_in] No hay session_id proporcionado, retornando False")
        return False
    
    base_url = PanaccessConfig.PANACCESS
    
    # Preparar payload
    payload = {
        "sessionId": session_id
    }
    
    # URL del endpoint
    url = f"{base_url}?f=cvLoggedIn&requestMode=function"
    
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    param_string = urlencode(payload)
    
    logger.debug("Verificando sesión")
    
    try:
        response = requests.post(
            url,
            data=param_string,
            headers=headers,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"Status code inesperado: {response.status_code}")
            raise PanAccessAPIError(
                f"Respuesta inesperada del servidor PanAccess: {response.status_code}",
                status_code=response.status_code
            )
        
        try:
            json_response = response.json()
        except ValueError as e:
            logger.error(f"Error parseando JSON: {str(e)}")
            raise PanAccessAPIError(
                f"Respuesta inválida del servidor PanAccess",
                status_code=response.status_code
            )
        
        success = json_response.get("success")
        
        if not success:
            error_message = json_response.get("errorMessage", "Sin mensaje de error")
            logger.debug(f"Sesión no válida: {error_message}")
            return False
        
        answer = json_response.get("answer")
        
        if isinstance(answer, bool):
            return answer
        elif isinstance(answer, str):
            return answer.lower() in ('true', '1', 'yes')
        else:
            logger.warning(f"Tipo de 'answer' inesperado: {type(answer).__name__}, asumiendo False")
            return False
        
    except requests.exceptions.Timeout:
        logger.error("Timeout verificando sesión (30s)")
        raise PanAccessTimeoutError(
            "Timeout al intentar verificar sesión con PanAccess. "
            "El servidor no respondió en 30 segundos."
        )
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Error conexión: {str(e)}")
        raise PanAccessConnectionError(
            f"Error de conexión con PanAccess: {str(e)}"
        )
    except (PanAccessTimeoutError, PanAccessConnectionError, PanAccessAPIError):
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}", exc_info=True)
        raise PanAccessAPIError(
            f"Error inesperado al verificar sesión con PanAccess: {str(e)}"
        )
