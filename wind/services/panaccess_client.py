"""
Cliente para interactuar con la API de PanAccess.
"""
import json
import logging
import time
import requests
from urllib.parse import urlencode
from typing import Dict, Any, Optional

from appConfig import PanaccessConfig
from wind.utils.panaccess_auth import login, logged_in
from wind.exceptions import (
    PanAccessException,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError,
    PanAccessSessionError
)

logger = logging.getLogger(__name__)


class PanAccessClient:
    """
    Cliente para interactuar con la API de PanAccess.
    """
    
    # Configuración de reintentos para errores de conexión/timeout
    MAX_RETRY_ATTEMPTS = 3
    INITIAL_RETRY_DELAY = 2  # segundos
    MAX_RETRY_DELAY = 30  # segundos
    
    def __init__(self, base_url: str = None):
        PanaccessConfig.validate()
        self.base_url = base_url or PanaccessConfig.PANACCESS
        self.session_id: Optional[str] = None
    
    def _summarize_answer(self, answer: Any) -> str:
        if answer is None:
            return "answer: None"
        
        answer_type = type(answer).__name__
        
        if isinstance(answer, dict):
            summary_parts = []
            
            for key in ['rows', 'smartcardEntries', 'productEntries']:
                if key in answer:
                    count = len(answer[key]) if isinstance(answer[key], list) else 0
                    summary_parts.append(f"{key}: {count} items")
            
            if 'records' in answer:
                summary_parts.append(f"records: {answer['records']}")
            
            if 'count' in answer:
                summary_parts.append(f"count: {answer['count']}")
            
            if not summary_parts:
                keys = list(answer.keys())
                summary_parts.append(f"{len(keys)} keys")
            
            return f"dict({', '.join(summary_parts)})"
        
        elif isinstance(answer, list):
            count = len(answer)
            return f"list({count} items)"
        
        elif isinstance(answer, str):
            if len(answer) > 50:
                return f"str({answer[:47]}...)"
            return f"str({answer})"
        
        else:
            return f"{answer_type}({str(answer)[:50]})"
    
    def authenticate(self) -> str:
        self.session_id = login()
        return self.session_id
    
    def _ensure_valid_session(self):
        if not self.session_id:
            self.authenticate()
            return
    
    def call(self, func_name: str, parameters: Dict[str, Any] = None, timeout: int = 60) -> Dict[str, Any]:
        """
        Llama a una función remota del API PanAccess con reintentos automáticos y backoff exponencial.
        """
        if parameters is None:
            parameters = {}
        
        # Asegurar sesión válida antes de hacer la llamada (excepto para login)
        if func_name != 'login' and func_name != 'cvLoggedIn':
            self._ensure_valid_session()
        
        # Preparar parámetros para logging (ocultar sessionId por seguridad)
        log_parameters = parameters.copy()
        if 'sessionId' in log_parameters:
            session_id_value = log_parameters['sessionId']
            if session_id_value:
                log_parameters['sessionId'] = f"{session_id_value[:20]}..." if len(str(session_id_value)) > 20 else "[REDACTED]"
        
        # Agregar sessionId a los parámetros si existe y no es login
        if self.session_id and func_name != 'login' and func_name != 'cvLoggedIn':
            parameters['sessionId'] = self.session_id
        
        # Construir URL
        url = f"{self.base_url}?f={func_name}&requestMode=function"
        
        # Serializar objetos complejos como JSON strings
        serialized_params = {}
        for key, value in parameters.items():
            if isinstance(value, (dict, list)):
                serialized_params[key] = json.dumps(value)
            else:
                serialized_params[key] = value
        
        # Preparar headers y datos
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        param_string = urlencode(serialized_params)
        
        logger.info(f"Llamando '{func_name}' - Parámetros: {log_parameters}")
        
        attempt = 0
        delay = self.INITIAL_RETRY_DELAY
        last_exception = None
        
        while attempt < self.MAX_RETRY_ATTEMPTS:
            try:
                response = requests.post(
                    url,
                    data=param_string,
                    headers=headers,
                    timeout=timeout
                )
                
                response.raise_for_status()
                
                try:
                    json_response = response.json()
                except ValueError as e:
                    logger.error(f"Error parseando JSON para '{func_name}': {str(e)}")
                    raise PanAccessAPIError(
                        f"Respuesta inválida del servidor PanAccess",
                        status_code=response.status_code
                    )
                
                success = json_response.get("success")
                
                if not success:
                    error_message = json_response.get("errorMessage", "Error desconocido")
                    error_code = (
                        json_response.get("errorCode")
                        or json_response.get("error_code")
                        or json_response.get("code")
                    )
                    logger.error(f"Llamada '{func_name}' falló: {error_message}")

                    if "session" in error_message.lower() or "logged" in error_message.lower():
                        logger.warning(
                            "Error de sesión detectado para '%s', limpiando sessionId",
                            func_name,
                        )
                        self.session_id = None
                        raise PanAccessSessionError(f"Error de sesión: {error_message}")

                    raise PanAccessAPIError(
                        f"Error en la respuesta de PanAccess: {error_message}",
                        status_code=response.status_code,
                        error_code=error_code,
                    )
                
                answer = json_response.get("answer")
                answer_summary = self._summarize_answer(answer)
                logger.info(f"Llamada '{func_name}' exitosa - {answer_summary}")
                
                return json_response
                
            except requests.exceptions.Timeout as e:
                attempt += 1
                last_exception = e
                logger.warning(f"Timeout '{func_name}' (intento {attempt}/{self.MAX_RETRY_ATTEMPTS})")
                
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.error(f"Timeout después de {self.MAX_RETRY_ATTEMPTS} intentos")
                    raise PanAccessTimeoutError(
                        f"Timeout al llamar a {func_name}. "
                        f"El servidor no respondió en {timeout} segundos después de {self.MAX_RETRY_ATTEMPTS} intentos."
                    )
                
                delay = min(delay * 2, self.MAX_RETRY_DELAY)
                logger.info(f"Reintentando en {delay}s...")
                time.sleep(delay)
                
            except requests.exceptions.ConnectionError as e:
                attempt += 1
                last_exception = e
                logger.warning(f"Error conexión '{func_name}' (intento {attempt}/{self.MAX_RETRY_ATTEMPTS})")
                
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.error(f"Error conexión después de {self.MAX_RETRY_ATTEMPTS} intentos")
                    raise PanAccessConnectionError(
                        f"Error de conexión con PanAccess después de {self.MAX_RETRY_ATTEMPTS} intentos: {str(e)}"
                    )
                
                delay = min(delay * 2, self.MAX_RETRY_DELAY)
                logger.info(f"Reintentando en {delay}s...")
                time.sleep(delay)
                
            except requests.exceptions.HTTPError as e:
                status_code = response.status_code if 'response' in locals() else None
                logger.error(f"Error HTTP '{func_name}': {str(e)} (Status: {status_code})")
                raise PanAccessAPIError(
                    f"Error HTTP al llamar a {func_name}: {str(e)}",
                    status_code=status_code
                )
            except (PanAccessSessionError, PanAccessAPIError):
                raise
            except Exception as e:
                logger.error(f"Error inesperado '{func_name}': {str(e)}", exc_info=True)
                raise PanAccessAPIError(
                    f"Error inesperado al llamar a {func_name}: {str(e)}"
                )
        
        if last_exception:
            if isinstance(last_exception, requests.exceptions.Timeout):
                raise PanAccessTimeoutError(
                    f"Timeout al llamar a {func_name} después de {self.MAX_RETRY_ATTEMPTS} intentos"
                )
            else:
                raise PanAccessConnectionError(
                    f"Error de conexión con PanAccess después de {self.MAX_RETRY_ATTEMPTS} intentos"
                )
    
    def logout(self) -> bool:
        if not self.session_id:
            return True
        
        try:
            result = self.call("cvLogout", {})
            self.session_id = None
            return result.get("success", False)
        except PanAccessException:
            self.session_id = None
            raise
    
    def is_authenticated(self) -> bool:
        return self.session_id is not None
    
    def check_session(self) -> bool:
        if not self.session_id:
            return False
        
        try:
            return logged_in(self.session_id)
        except PanAccessException:
            self.session_id = None
            return False
