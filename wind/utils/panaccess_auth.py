"""
Funciones de autenticación con PanAccess.
"""
import hashlib
import logging
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlencode

from appConfig import PanaccessConfig
from wind.exceptions import (
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError,
    PanAccessRateLimitError,
)

logger = logging.getLogger(__name__)

# Palabras clave para detectar el límite de PanAccess de ~20 logins en 5
# minutos a partir del texto de error (la API no documenta un errorCode
# específico para este caso). Ajustar/confirmar esta lista contra la
# documentación oficial de PanAccess si el mensaje real difiere.
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "too many",
    "demasiados intentos",
    "muchos intentos",
    "límite de intentos",
    "limite de intentos",
    "try again later",
    "intente más tarde",
    "intenta más tarde",
)


def _is_rate_limit_error(error_message: str) -> bool:
    if not error_message:
        return False
    text = error_message.lower()
    return any(keyword in text for keyword in _RATE_LIMIT_KEYWORDS)


# Sesión HTTP compartida hacia PanAccess (login, verificación de sesión, y
# todas las llamadas de PanAccessClient.call) -- antes cada request abría y
# cerraba su propia conexión TCP+TLS con `requests.post(...)` suelto, sin
# reutilizar nada. Con una única `requests.Session` con pool de conexiones
# keep-alive, requests hacia el mismo host reutilizan el socket/TLS ya
# establecido en vez de renegociar TLS en cada llamada -- reduce latencia y
# carga en el servidor de PanAccess bajo tráfico sostenido. Un solo objeto
# por proceso, creado perezosamente y protegido con lock (double-checked
# locking) para que sea seguro entre threads (workers Gunicorn/Celery con
# threads, ThreadPoolExecutor de sync de smartcards, etc.).
_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_panaccess_session() -> requests.Session:
    """Sesión `requests` compartida y reutilizable para hablar con PanAccess."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = requests.Session()
                adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _session = session
    return _session


def hash_password(password: str, salt: str = None) -> str:
    if salt is None:
        salt = PanaccessConfig.SALT
    
    return hashlib.md5((password + salt).encode()).hexdigest()


def _call_panaccess_function(func_name: str, payload: dict) -> dict:
    """
    POST genérico contra una función de PanAccess (``f=<func_name>``).

    Antes, `login()` y `logged_in()` duplicaban exactamente esta parte: la
    construcción de la URL, el POST con la sesión compartida, la validación
    de status HTTP, el parseo de JSON y el mapeo de errores de red/timeout a
    las excepciones propias del proyecto -- solo diferían en qué hacían con
    el resultado. Consolidarlo acá evita que un fix futuro (como el de la
    clasificación de errores de login) se aplique en una función y se olvide
    en la otra, que es justo lo que ya había pasado antes.

    Devuelve el JSON ya parseado; cada llamador decide qué hacer con
    ``success``/``answer``/``errorMessage`` según su propia lógica.
    """
    PanaccessConfig.validate()
    base_url = PanaccessConfig.PANACCESS
    url = f"{base_url}?f={func_name}&requestMode=function"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    param_string = urlencode(payload)

    try:
        response = get_panaccess_session().post(
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
            return response.json()
        except ValueError as e:
            logger.error(f"Error parseando JSON: {str(e)}")
            raise PanAccessAPIError(
                f"Respuesta inválida del servidor PanAccess",
                status_code=response.status_code
            )

    except requests.exceptions.Timeout:
        logger.error(f"Timeout al intentar '{func_name}' (30s)")
        raise PanAccessTimeoutError(
            f"Timeout al intentar conectarse con PanAccess ({func_name}). "
            "El servidor no respondió en 30 segundos."
        )
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Error conexión ({func_name}): {str(e)}")
        raise PanAccessConnectionError(
            f"Error de conexión con PanAccess ({func_name}): {str(e)}"
        )
    except (PanAccessAuthenticationError, PanAccessAPIError, PanAccessTimeoutError,
            PanAccessConnectionError, PanAccessRateLimitError):
        raise
    except Exception as e:
        logger.error(f"Error inesperado ({func_name}): {str(e)}", exc_info=True)
        raise PanAccessAPIError(
            f"Error inesperado al intentar '{func_name}' con PanAccess: {str(e)}"
        )


def login() -> str:
    """
    Realiza login en PanAccess y retorna el sessionId.
    """
    # Validar acá también (además de adentro de _call_panaccess_function):
    # antes de este refactor, validate() corría primero y podía informar de
    # una sola vez TODAS las variables de entorno faltantes (URL, SALT,
    # HCID, etc.), no solo username/password/api_token. Si se dejara que
    # solo corriera dentro de _call_panaccess_function, el chequeo de abajo
    # (specific a estos 3 campos) se adelantaría y ocultaría cualquier otra
    # variable faltante hasta un segundo intento.
    PanaccessConfig.validate()

    username = PanaccessConfig.USERNAME
    password = PanaccessConfig.PASSWORD
    api_token = PanaccessConfig.API_TOKEN

    if not username or not password or not api_token:
        raise PanAccessAuthenticationError(
            "Faltan credenciales de PanAccess en la configuración. "
            "Verifica las variables de entorno: username, password, api_token"
        )

    hashed_password = hash_password(password)
    payload = {
        "username": username,
        "password": hashed_password,
        "apiToken": api_token
    }

    logger.info("Iniciando login")
    json_response = _call_panaccess_function("login", payload)

    success = json_response.get("success")

    if not success:
        error_message = json_response.get("errorMessage", "Login fallido sin mensaje explícito")
        answer = json_response.get("answer")
        logger.error(f"Login fallido: {error_message}")

        if _is_rate_limit_error(error_message):
            logger.error(
                "Login rechazado por límite de intentos de PanAccess (rate limit): %s",
                error_message,
            )
            raise PanAccessRateLimitError(
                f"Límite de intentos de login excedido en PanAccess: {error_message}"
            )

        # Solo `answer == "false"` (la señal explícita de PanAccess para
        # credenciales inválidas) dispara `PanAccessAuthenticationError`;
        # cualquier otro fallo cae al `PanAccessAPIError` genérico (ver
        # auditoría -- antes `or error_message` hacía que esta rama
        # capturara casi cualquier error, no solo los de autenticación).
        if answer == "false":
            raise PanAccessAuthenticationError(
                f"Error de autenticación: {error_message}"
            )

        raise PanAccessAPIError(
            f"Error en la respuesta de PanAccess: {error_message}"
        )

    session_id = json_response.get("answer")

    if not session_id:
        logger.error("No se recibió sessionId en la respuesta")
        raise PanAccessAPIError(
            "Login exitoso pero no se recibió sessionId en la respuesta"
        )

    logger.info(f"Login exitoso - SessionId obtenido ({len(session_id)} caracteres)")
    return session_id


def logged_in(session_id: str) -> bool:
    """
    Verifica si un sessionId de PanAccess sigue siendo válido.
    """
    if not session_id:
        logger.debug("🔍 [logged_in] No hay session_id proporcionado, retornando False")
        return False

    payload = {"sessionId": session_id}
    logger.debug("Verificando sesión")

    json_response = _call_panaccess_function("cvLoggedIn", payload)

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
