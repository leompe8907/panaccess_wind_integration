"""
Cliente singleton thread-safe para PanAccess.
"""
import threading
import time
import logging
from typing import Optional

from wind.services.panaccess_client import PanAccessClient
from wind.services import panaccess_circuit_breaker
from wind.services import panaccess_session_store
from wind.utils.panaccess_auth import login, logged_in
from wind.exceptions import (
    PanAccessException,
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError,
    PanAccessSessionError,
)

logger = logging.getLogger(__name__)


class PanAccessSingleton:
    """
    Singleton thread-safe para el cliente PanAccess.
    """
    
    _instance = None
    _lock = threading.Lock()  # Lock para inicialización
    _session_lock = threading.RLock()  # Reentrant lock para sesión
    
    # Configuración de reintentos
    MAX_RETRY_ATTEMPTS = 5
    INITIAL_RETRY_DELAY = 1  # segundos
    MAX_RETRY_DELAY = 60  # segundos
    ALERT_AFTER_ATTEMPTS = 3  # Enviar alerta después de X intentos
    
    # Configuración de validación periódica
    VALIDATION_INTERVAL = 900  # Validar cada 15 minutos (900 segundos)
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(PanAccessSingleton, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.client = PanAccessClient()
        self._initialized = True
        self._retry_count = 0
        self._last_alert_sent = False
        self._validation_thread = None
        self._stop_validation = threading.Event()
        
    def _authenticate_with_retry(self) -> str:
        """
        Intenta autenticarse con reintentos y backoff exponencial.
        """
        attempt = 0
        delay = self.INITIAL_RETRY_DELAY
        
        while attempt < self.MAX_RETRY_ATTEMPTS:
            try:
                logger.info(f"Intento login #{attempt + 1}/{self.MAX_RETRY_ATTEMPTS}")
                session_id = login()
                
                self._retry_count = 0
                self._last_alert_sent = False
                logger.info("Login exitoso")
                panaccess_session_store.set_session_id(session_id)
                return session_id
                
            except (PanAccessAuthenticationError, PanAccessConnectionError, PanAccessTimeoutError) as e:
                attempt += 1
                self._retry_count = attempt
                
                # Enviar alerta después de X intentos
                if attempt >= self.ALERT_AFTER_ATTEMPTS and not self._last_alert_sent:
                    self._send_alert(attempt, str(e))
                    self._last_alert_sent = True
                
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.error(f"Login falló después de {self.MAX_RETRY_ATTEMPTS} intentos")
                    raise PanAccessException(
                        f"Error de autenticación después de {self.MAX_RETRY_ATTEMPTS} intentos: {str(e)}"
                    )
                
                delay = min(delay * 2, self.MAX_RETRY_DELAY)
                logger.warning(f"Login falló (intento {attempt}/{self.MAX_RETRY_ATTEMPTS}), reintentando en {delay}s")
                
                time.sleep(delay)
            
            except PanAccessException as e:
                raise
            except Exception as e:
                attempt += 1
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.error(f"Error inesperado después de {attempt} intentos: {str(e)}")
                    raise PanAccessException(f"Error inesperado en login: {str(e)}")
                
                delay = min(delay * 2, self.MAX_RETRY_DELAY)
                logger.warning(f"Error inesperado (intento {attempt}/{self.MAX_RETRY_ATTEMPTS}), reintentando en {delay}s")
                time.sleep(delay)
        
        raise PanAccessException("Error crítico: no se pudo autenticar después de múltiples intentos")
    
    def _send_alert(self, attempt: int, error_message: str):
        alert_message = (
            f"ALERTA: PanAccess login ha fallado {attempt} veces. "
            f"Último error: {error_message}. "
            f"El sistema seguirá intentando hasta {self.MAX_RETRY_ATTEMPTS} intentos."
        )
        logger.error(alert_message)

        try:
            import sentry_sdk
            sentry_sdk.capture_message(alert_message, level="error")
        except Exception:
            pass
        
    def _load_or_authenticate_session(self) -> None:
        if self.client.session_id:
            return

        stored = panaccess_session_store.get_session_id()
        if stored:
            self.client.session_id = stored
            return

        if panaccess_session_store.is_enabled():
            with panaccess_session_store.refresh_lock() as acquired:
                if not acquired:
                    stored = panaccess_session_store.get_session_id()
                    if stored:
                        self.client.session_id = stored
                        return
                if not self.client.session_id:
                    stored = panaccess_session_store.get_session_id()
                    if stored:
                        self.client.session_id = stored
                    else:
                        logger.info("No hay sesión, autenticando...")
                        self.client.session_id = self._authenticate_with_retry()
            return

        logger.info("No hay sesión, autenticando...")
        self.client.session_id = self._authenticate_with_retry()

    def ensure_session(self):
        with self._session_lock:
            self._load_or_authenticate_session()
            return
    
    def call(self, func_name: str, parameters: dict = None, timeout: int = 60) -> dict:
        """
        Llama a una función de la API PanAccess de manera segura y controlada.
        """
        def _invoke():
            try:
                return self._call_once(func_name, parameters, timeout)
            except PanAccessSessionError:
                logger.warning(
                    "Sesión PanAccess inválida en '%s', reautenticando y reintentando...",
                    func_name,
                )
                with self._session_lock:
                    self.client.session_id = None
                    panaccess_session_store.clear_session_id()
                    self.ensure_session()
                return self._call_once(func_name, parameters, timeout)

        if panaccess_circuit_breaker.circuit_breaker_enabled():
            return panaccess_circuit_breaker.get_circuit_breaker().execute(_invoke)
        return _invoke()

    def _call_once(self, func_name: str, parameters: dict = None, timeout: int = 60) -> dict:
        if func_name not in ('login', 'cvLoggedIn'):
            if not self.client.session_id:
                logger.warning("No hay sesión activa, obteniendo una...")
                self.ensure_session()
        return self.client.call(func_name, parameters, timeout)
    
    def get_client(self) -> PanAccessClient:
        return self.client
    
    def reset_session(self):
        with self._session_lock:
            self.client.session_id = None
            panaccess_session_store.clear_session_id()
            logger.info("Sesión reseteada")
    
    def _periodic_validation(self):
        logger.info(f"Validación periódica iniciada (intervalo: {self.VALIDATION_INTERVAL}s)")
        
        while not self._stop_validation.is_set():
            try:
                if self._stop_validation.wait(timeout=self.VALIDATION_INTERVAL):
                    break
                
                with self._session_lock:
                    if not self.client.session_id:
                        continue
                    
                    try:
                        is_valid = logged_in(self.client.session_id)
                        if not is_valid:
                            logger.info("Sesión caducada, refrescando...")
                            panaccess_session_store.clear_session_id()
                            self.client.session_id = self._authenticate_with_retry()
                    except (PanAccessConnectionError, PanAccessTimeoutError) as e:
                        logger.warning(f"⚠️ Error de conexión en validación periódica: {str(e)}. Manteniendo sesión actual.")
                    except PanAccessAPIError as e:
                        error_code = getattr(e, 'error_code', None)
                        if error_code == 'no_access_to_function':
                            logger.debug("⚠️ Error de permisos en validación periódica, manteniendo sesión")
                        else:
                            logger.warning(f"⚠️ Error de API en validación periódica: {str(e)}. Intentando refrescar...")
                            try:
                                self.client.session_id = self._authenticate_with_retry()
                            except Exception:
                                logger.error("❌ Error al refrescar sesión en validación periódica")
                
                logger.debug("✅ Validación periódica completada")
                
            except Exception as e:
                logger.error(f"❌ Error en validación periódica: {str(e)}")
        
        logger.info("Validación periódica detenida")
    
    def start_periodic_validation(self):
        if self._validation_thread is not None and self._validation_thread.is_alive():
            logger.warning("Thread de validación ya está corriendo")
            return
        
        self.stop_periodic_validation()
        
        self._stop_validation.clear()
        self._validation_thread = threading.Thread(
            target=self._periodic_validation,
            name="PanAccessValidationThread",
            daemon=True
        )
        self._validation_thread.start()
        logger.info("Thread de validación periódica iniciado")
    
    def stop_periodic_validation(self):
        if self._validation_thread is not None and self._validation_thread.is_alive():
            logger.info("🛑 Deteniendo thread de validación periódica...")
            self._stop_validation.set()
            self._validation_thread.join(timeout=5)
            self._validation_thread = None


_panaccess_singleton: Optional[PanAccessSingleton] = None


def get_panaccess() -> PanAccessSingleton:
    global _panaccess_singleton
    if _panaccess_singleton is None:
        _panaccess_singleton = PanAccessSingleton()
    return _panaccess_singleton


def initialize_panaccess():
    singleton = get_panaccess()
    try:
        singleton.ensure_session()
        logger.info("PanAccess inicializado y autenticado")
        singleton.start_periodic_validation()
        
    except PanAccessException as e:
        logger.error(f"Error inicializando PanAccess: {str(e)}")
        logger.warning("El sistema intentará autenticarse en el primer request")
        
        try:
            singleton.start_periodic_validation()
        except Exception as ve:
            logger.error(f"Error iniciando validación periódica: {str(ve)}")
