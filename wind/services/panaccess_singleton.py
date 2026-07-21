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
    PanAccessRateLimitError,
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
        # Igual que __new__, protegido por el lock de clase: sin esto, dos
        # hilos pidiendo el singleton por primera vez casi al mismo tiempo
        # pueden ver ambos "todavía no inicializado" y pisarse el cliente y
        # el resto del estado interno entre sí (condición de carrera única
        # en el primer arranque concurrente del proceso).
        with self.__class__._lock:
            if self._initialized:
                return

            self.client = PanAccessClient()
            self._retry_count = 0
            self._last_alert_sent = False
            self._validation_thread = None
            self._stop_validation = threading.Event()
            self._initialized = True
        
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
                
            # PanAccessAPIError se suma acá (antes no estaba) como
            # consecuencia directa de corregir la clasificación en
            # wind/utils/panaccess_auth.py: antes, prácticamente cualquier
            # fallo de login (no solo credenciales inválidas) se reportaba
            # como PanAccessAuthenticationError, así que quedaba cubierto
            # por este mismo reintento "sin querer". Al corregir la
            # clasificación, un error genérico de API durante el login
            # ahora sale como PanAccessAPIError -- si no se agregaba acá,
            # ese tipo de fallo dejaría de reintentarse y de tener el
            # backoff/alerta que ya existían, en vez de solo quedar mejor
            # clasificado en los logs.
            except (
                PanAccessAuthenticationError,
                PanAccessConnectionError,
                PanAccessTimeoutError,
                PanAccessAPIError,
            ) as e:
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
            
            except PanAccessRateLimitError as e:
                # PanAccess ya está rechazando logins por exceso de intentos
                # (rate limit propio, ~20 logins/5min). Reintentar de
                # inmediato con el backoff corto de este método solo
                # empeoraría el bloqueo, así que no se reintenta aquí: se
                # deja constancia clara en el log y se propaga el error para
                # que el llamador (o el circuit breaker) decida cómo
                # degradar en vez de seguir insistiendo.
                logger.error(
                    "PanAccess en rate limit de logins; no se reintenta "
                    "automáticamente para no empeorar el bloqueo: %s",
                    str(e),
                )
                raise
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

        if not panaccess_session_store.is_enabled():
            logger.info("No hay sesión, autenticando...")
            self.client.session_id = self._authenticate_with_retry()
            return

        # Con sesión compartida en Redis: sólo un proceso debe autenticarse
        # a la vez. El lock es bloqueante (espera hasta blocking_timeout) para
        # que los procesos que no lo consiguen le den tiempo al que sí lo
        # tiene a publicar el sessionId, en vez de autenticarse también.
        with panaccess_session_store.refresh_lock(blocking=True, blocking_timeout=15.0) as acquired:
            # Pudo haberse publicado una sesión mientras esperábamos el lock
            # (la hayamos adquirido o no) — siempre se revisa primero.
            stored = panaccess_session_store.get_session_id()
            if stored:
                self.client.session_id = stored
                return

            if not acquired:
                # Ni conseguimos el lock ni apareció una sesión tras esperar:
                # el proceso que lo tiene puede haber fallado o estar
                # tardando más de lo esperado. Es un caso degradado y poco
                # frecuente (no la carrera rutinaria de antes) — se deja
                # constancia en el log y se autentica como último recurso
                # para no dejar el sistema sin sesión indefinidamente.
                logger.warning(
                    "No se pudo adquirir el lock de sesión PanAccess tras "
                    "esperar %.0fs y no hay sesión publicada; autenticando "
                    "como último recurso.",
                    15.0,
                )

            logger.info("No hay sesión, autenticando...")
            self.client.session_id = self._authenticate_with_retry()

    def ensure_session(self):
        with self._session_lock:
            self._load_or_authenticate_session()
            return
    
    def call(self, func_name: str, parameters: dict = None, timeout: int = None) -> dict:
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

    def _call_once(self, func_name: str, parameters: dict = None, timeout: int = None) -> dict:
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

                # Sólo se lee el session_id actual bajo lock (operación
                # instantánea). La llamada de red a PanAccess y una eventual
                # reautenticación se hacen SIN sostener _session_lock, para
                # no bloquear el resto de la aplicación (que también usa
                # este lock en cada llamada) durante lo que puede tardar
                # hasta ~150s en el peor caso.
                with self._session_lock:
                    current_session_id = self.client.session_id

                if not current_session_id:
                    continue

                needs_refresh = False
                try:
                    is_valid = logged_in(current_session_id)
                    needs_refresh = not is_valid
                except (PanAccessConnectionError, PanAccessTimeoutError) as e:
                    logger.warning(f"⚠️ Error de conexión en validación periódica: {str(e)}. Manteniendo sesión actual.")
                    continue
                except PanAccessAPIError as e:
                    error_code = getattr(e, 'error_code', None)
                    if error_code == 'no_access_to_function':
                        logger.debug("⚠️ Error de permisos en validación periódica, manteniendo sesión")
                        continue
                    logger.warning(f"⚠️ Error de API en validación periódica: {str(e)}. Intentando refrescar...")
                    needs_refresh = True

                if not needs_refresh:
                    logger.debug("✅ Validación periódica completada")
                    continue

                logger.info("Sesión caducada o inválida, refrescando...")
                try:
                    new_session_id = self._authenticate_with_retry()
                except Exception:
                    logger.error("❌ Error al refrescar sesión en validación periódica")
                    continue

                with self._session_lock:
                    # Sólo reemplazar si nadie más (otro hilo/petición) ya
                    # refrescó la sesión mientras autenticábamos sin el lock
                    # — evita pisar una sesión más nueva con una más vieja.
                    if self.client.session_id == current_session_id:
                        self.client.session_id = new_session_id
                    else:
                        logger.debug(
                            "La sesión ya había sido refrescada por otra vía; "
                            "se descarta este login redundante."
                        )

                logger.debug("✅ Validación periódica completada (sesión refrescada)")

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
