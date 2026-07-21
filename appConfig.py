"""
Configuración centralizada desde variables de entorno (.env).
Todas las variables configurables del proyecto deben declararse aquí.
settings.py y el resto del código importan desde estas clases.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

# Antes: `load_dotenv(override=True)` seguido de `load_dotenv()` -- la
# segunda llamada es un no-op total (mismo archivo, sin override, todo lo
# que carga ya lo puso la primera), y ninguna de las dos apunta a una ruta
# explícita: `load_dotenv()` busca un `.env` subiendo desde el directorio
# de trabajo actual, no desde la ubicación real de este archivo. Si el
# proceso arranca con un CWD distinto a la raíz del proyecto (ej. un
# systemd unit sin `WorkingDirectory`, o un cron), podía no encontrar el
# `.env` correcto. Se ancla explícitamente a la carpeta de este archivo
# (la raíz del proyecto, igual que `BASE_DIR` en settings.py) para que
# funcione igual sin importar desde dónde se invoque el proceso.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_env(value: str | None) -> str:
    """Quita espacios y comillas envueltas (p. ej. REDIS_PASSWORD="")."""
    if not value:
        return ""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Variable de entorno %s='%s' no es un número válido, usando default=%s",
            name, raw, default,
        )
        return default


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _normalize_host(value: str) -> str:
    """ALLOWED_HOSTS: solo hostname, sin esquema ni path."""
    v = value.strip()
    for prefix in ("https://", "http://"):
        if v.lower().startswith(prefix):
            v = v[len(prefix) :]
    return v.strip("/")


def _normalize_origin(value: str) -> str:
    """CORS: origen sin barra final."""
    return value.strip().rstrip("/")


def _normalize_csrf_origin(value: str) -> str:
    """CSRF_TRUSTED_ORIGINS: Django exige esquema explícito (https://dominio)."""
    v = value.strip().rstrip("/")
    if not v:
        return v
    if not (v.startswith("http://") or v.startswith("https://")):
        v = f"https://{v}"
    return v


# ---------------------------------------------------------------------------
# Django / seguridad HTTP
# ---------------------------------------------------------------------------

class DjangoConfig:
    SECRET_KEY = _strip_env(os.getenv("SECRET_KEY"))
    DEBUG = _env_bool("DEBUG", False)
    ALLOWED_HOSTS = [_normalize_host(h) for h in _csv("ALLOWED_HOSTS")]
    # Valor crudo de la variable (None si no está definida en el entorno) —
    # se necesita distinguir "no configurada" de "configurada en false" para
    # poder avisar cuando alguien la desactiva a propósito en producción.
    PRODUCTION_HTTPS_RAW = os.getenv("PRODUCTION_HTTPS")
    SYNC_ADMIN_IP_ALLOWLIST = _csv("SYNC_ADMIN_IP_ALLOWLIST")
    # Dominios reales del frontend en producción (con esquema, ej.
    # "https://app.wind.do"). Sin esto, Django 4+ rechaza con 403 CSRF
    # cualquier request con cookie+CSRF (admin, /accounts/ de allauth) que
    # llegue con Origin/Referer distinto al host de Django mismo.
    CSRF_TRUSTED_ORIGINS = [_normalize_csrf_origin(o) for o in _csv("CSRF_TRUSTED_ORIGINS")]

    @classmethod
    def production_https(cls, *, debug: bool) -> bool:
        """
        Si aplicar HSTS, cookies seguras y redirect SSL.

        Por defecto se activa automáticamente cuando DEBUG=False, en vez de
        depender de que alguien recuerde definir PRODUCTION_HTTPS=true (antes
        el default era False siempre, así que un despliegue en producción sin
        esa variable quedaba sin estas protecciones sin ningún aviso).

        Se puede seguir desactivando explícitamente con PRODUCTION_HTTPS=false
        (p. ej. un staging con DEBUG=False detrás de un balanceador que ya
        termina TLS y aplica sus propias cabeceras) — settings.py deja
        constancia en el log cuando se hace esa combinación explícita.
        """
        if cls.PRODUCTION_HTTPS_RAW is not None:
            return _env_bool("PRODUCTION_HTTPS", False)
        return not debug

    @classmethod
    def production_https_explicitly_disabled(cls, *, debug: bool) -> bool:
        """True si con DEBUG=False alguien puso PRODUCTION_HTTPS=false a propósito."""
        return (
            not debug
            and cls.PRODUCTION_HTTPS_RAW is not None
            and not _env_bool("PRODUCTION_HTTPS", False)
        )

    @classmethod
    def validate(cls):
        missing = []
        if not cls.SECRET_KEY:
            missing.append("SECRET_KEY")
        if not cls.ALLOWED_HOSTS:
            missing.append("ALLOWED_HOSTS")
        if missing:
            raise EnvironmentError(f"❌ Faltan variables de entorno: {', '.join(missing)}")


class CorsConfig:
    ALLOWED_ORIGINS = [_normalize_origin(o) for o in _csv("CORS_ALLOWED_ORIGINS")]
    DEV_DEFAULTS = _env_bool("CORS_DEV_DEFAULTS", False)
    ALLOW_CREDENTIALS = _env_bool("CORS_ALLOW_CREDENTIALS", False)

    DEV_ORIGINS = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @classmethod
    def resolved_origins(cls, *, debug: bool) -> list[str]:
        if cls.ALLOWED_ORIGINS:
            return cls.ALLOWED_ORIGINS
        if debug or cls.DEV_DEFAULTS:
            return cls.DEV_ORIGINS
        return []

    @classmethod
    def validate_no_allow_all(cls) -> None:
        if _env_bool("CORS_ALLOW_ALL_ORIGINS", False):
            raise EnvironmentError(
                "CORS_ALLOW_ALL_ORIGINS no está permitido. "
                "Use CORS_ALLOWED_ORIGINS con dominios concretos."
            )


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

class DatabaseConfig:
    ENGINE = _strip_env(os.getenv("DB_ENGINE"))
    NAME = _strip_env(os.getenv("DB_NAME"))
    USER = _strip_env(os.getenv("DB_USER"))
    PASSWORD = _strip_env(os.getenv("DB_PASSWORD"))
    HOST = _strip_env(os.getenv("DB_HOST"))
    PORT = _strip_env(os.getenv("DB_PORT"))
    # Antes por defecto era 0 (sin conexiones persistentes): cada request y
    # cada tarea Celery abría y cerraba una conexión nueva a Postgres. Con
    # PgBouncer/pooler externo puede seguir siendo 0 a propósito; si no hay
    # pooler externo, mantener conexiones vivas ~60s reduce notablemente el
    # overhead de TCP/TLS+auth por request. Sigue siendo configurable por env.
    CONN_MAX_AGE = _env_int("DB_CONN_MAX_AGE", 60)
    # Solo tiene efecto si CONN_MAX_AGE > 0: valida (SELECT 1 barato) que la
    # conexión reutilizada siga viva antes de usarla, en vez de fallar el
    # request si Postgres la cerró por su cuenta (idle timeout, reinicio, etc).
    CONN_HEALTH_CHECKS = _env_bool("DB_CONN_HEALTH_CHECKS", True)

    REPLICA_HOST = _strip_env(os.getenv("DB_REPLICA_HOST"))
    REPLICA_PORT = _strip_env(os.getenv("DB_REPLICA_PORT"))

    @classmethod
    def use_postgresql(cls) -> bool:
        engine = (cls.ENGINE or "").lower()
        return "postgresql" in engine or engine == "postgres"

    @classmethod
    def configure(cls):
        if not cls.use_postgresql():
            return cls
        missing = []
        if not cls.ENGINE:
            missing.append("DB_ENGINE")
        if not cls.NAME:
            missing.append("DB_NAME")
        if not cls.USER:
            missing.append("DB_USER")
        if cls.PASSWORD is None or cls.PASSWORD == "":
            missing.append("DB_PASSWORD")
        if not cls.HOST:
            missing.append("DB_HOST")
        if not cls.PORT:
            missing.append("DB_PORT")
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        return cls

    @classmethod
    def django_default_database(cls) -> dict:
        cls.configure()
        db = {
            "ENGINE": cls.ENGINE,
            "NAME": cls.NAME,
            "USER": cls.USER,
            "PASSWORD": cls.PASSWORD,
            "HOST": cls.HOST,
            "PORT": cls.PORT,
        }
        if cls.CONN_MAX_AGE:
            db["CONN_MAX_AGE"] = cls.CONN_MAX_AGE
            db["CONN_HEALTH_CHECKS"] = cls.CONN_HEALTH_CHECKS
        return db

    @classmethod
    def django_replica_database(cls) -> dict | None:
        if not cls.use_postgresql() or not cls.REPLICA_HOST:
            return None
        base = cls.django_default_database()
        return {
            **base,
            "HOST": cls.REPLICA_HOST,
            "PORT": cls.REPLICA_PORT or base.get("PORT"),
        }


# ---------------------------------------------------------------------------
# Redis / Celery
# ---------------------------------------------------------------------------

class RedisConfig:
    """
    Redis: broker Celery, locks, sesión PanAccess y caché Django (DB distinta).
    Prioridad broker: CELERY_BROKER_URL > REDIS_URL > REDIS_HOST/PORT/DB/PASSWORD.
    """

    HOST = _strip_env(os.getenv("REDIS_HOST")) or "localhost"
    PORT = _env_int("REDIS_PORT", 6379)
    DB = max(0, min(15, _env_int("REDIS_DB", 0)))
    PASSWORD = _strip_env(os.getenv("REDIS_PASSWORD"))
    CACHE_DB = max(0, min(15, _env_int("REDIS_CACHE_DB", 1)))

    # TLS opcional (Redis 6+ con soporte TLS nativo, o stunnel delante).
    # Desactivado por defecto para no romper despliegues existentes contra
    # Redis en red privada/localhost sin TLS configurado del otro lado.
    SSL = _env_bool("REDIS_SSL", False)
    # required (default, verifica el certificado del servidor) | optional |
    # none (sin verificar -- solo para diagnósticos, no usar en producción).
    SSL_CERT_REQS = (_strip_env(os.getenv("REDIS_SSL_CERT_REQS")) or "required").lower()

    REDIS_URL = _strip_env(os.getenv("REDIS_URL"))
    CELERY_BROKER_URL = _strip_env(os.getenv("CELERY_BROKER_URL"))
    CELERY_RESULT_BACKEND = _strip_env(os.getenv("CELERY_RESULT_BACKEND"))

    @classmethod
    def build_url(cls, db: int | None = None) -> str:
        database = cls.DB if db is None else db
        auth = f":{quote(cls.PASSWORD, safe='')}@" if cls.PASSWORD else ""
        scheme = "rediss" if cls.SSL else "redis"
        url = f"{scheme}://{auth}{cls.HOST}:{cls.PORT}/{database}"
        if cls.SSL and cls.SSL_CERT_REQS != "required":
            url += f"?ssl_cert_reqs={cls.SSL_CERT_REQS}"
        return url

    @classmethod
    def broker_url(cls) -> str:
        return cls.CELERY_BROKER_URL or cls.REDIS_URL or cls.build_url()

    @classmethod
    def result_backend_url(cls) -> str:
        return cls.CELERY_RESULT_BACKEND or cls.broker_url()

    @classmethod
    def celery_eager(cls) -> bool:
        return _env_bool("CELERY_TASK_ALWAYS_EAGER", False)

    @classmethod
    def celery_worker_pool(cls) -> str:
        explicit = _strip_env(os.getenv("CELERY_WORKER_POOL"))
        if explicit:
            return explicit
        return "solo" if sys.platform == "win32" else "prefork"

    @classmethod
    def get_client(cls):
        from redis import Redis

        kwargs = {
            "host": cls.HOST,
            "port": cls.PORT,
            "db": cls.DB,
            "decode_responses": False,
        }
        if cls.PASSWORD:
            kwargs["password"] = cls.PASSWORD
        if cls.SSL:
            import ssl as _ssl

            cert_reqs_map = {
                "required": _ssl.CERT_REQUIRED,
                "optional": _ssl.CERT_OPTIONAL,
                "none": _ssl.CERT_NONE,
            }
            kwargs["ssl"] = True
            kwargs["ssl_cert_reqs"] = cert_reqs_map.get(cls.SSL_CERT_REQS, _ssl.CERT_REQUIRED)
        return Redis(**kwargs)

    FULL_SYNC_FLAG_KEY = "celery:flag:full_sync_in_progress"
    SMARTCARD_INCREMENTAL_SINCE_KEY = "celery:smartcard_sync:last_incremental_at"
    SMARTCARD_FULL_BY_SUBSCRIBER_AT_KEY = "celery:smartcard_sync:last_full_by_subscriber_at"
    _eager_full_sync_active = False
    _eager_smartcard_incremental_since: str | None = None
    _eager_smartcard_full_by_subscriber_at: str | None = None

    @classmethod
    def set_full_sync_in_progress(cls, *, timeout: int = 3600) -> None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            cls._eager_full_sync_active = True
            return
        cls.get_client().set(cls.FULL_SYNC_FLAG_KEY, b"1", ex=max(60, int(timeout)))

    @classmethod
    def clear_full_sync_in_progress(cls) -> None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            cls._eager_full_sync_active = False
            return
        try:
            cls.get_client().delete(cls.FULL_SYNC_FLAG_KEY)
        except Exception:
            pass

    @classmethod
    def is_full_sync_in_progress(cls) -> bool:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return bool(getattr(cls, "_eager_full_sync_active", False))
        try:
            return cls.get_client().get(cls.FULL_SYNC_FLAG_KEY) is not None
        except Exception:
            return False

    @classmethod
    def get_smartcard_incremental_since(cls) -> str | None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return cls._eager_smartcard_incremental_since
        try:
            raw = cls.get_client().get(cls.SMARTCARD_INCREMENTAL_SINCE_KEY)
            if raw is None:
                return None
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception:
            return None

    @classmethod
    def set_smartcard_incremental_since(cls, value: str) -> None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            cls._eager_smartcard_incremental_since = value
            return
        try:
            cls.get_client().set(cls.SMARTCARD_INCREMENTAL_SINCE_KEY, value.encode("utf-8"))
        except Exception:
            pass

    @classmethod
    def get_smartcard_full_by_subscriber_at(cls) -> str | None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return cls._eager_smartcard_full_by_subscriber_at
        try:
            raw = cls.get_client().get(cls.SMARTCARD_FULL_BY_SUBSCRIBER_AT_KEY)
            if raw is None:
                return None
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception:
            return None

    @classmethod
    def set_smartcard_full_by_subscriber_at(cls, value: str) -> None:
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            cls._eager_smartcard_full_by_subscriber_at = value
            return
        try:
            cls.get_client().set(
                cls.SMARTCARD_FULL_BY_SUBSCRIBER_AT_KEY, value.encode("utf-8")
            )
        except Exception:
            pass

    @classmethod
    @contextmanager
    def task_lock(
        cls,
        key: str,
        *,
        timeout: int = 600,
        blocking: bool = False,
        blocking_timeout: float | None = None,
        auto_extend: bool = False,
    ):
        """Lock distribuido en Redis.

        Por defecto (``blocking=False``) se comporta como antes: intenta
        adquirir el lock una sola vez y no espera — usado por las tareas
        Celery de sync, que deben saltarse si otra instancia ya está
        corriendo, no esperar a que termine.

        Con ``blocking=True`` espera hasta ``blocking_timeout`` segundos a
        que el lock se libere antes de rendirse. Lo usa
        ``panaccess_session_store.refresh_lock()`` para que los procesos que
        no consiguen el lock esperen a que el que sí lo tiene termine de
        autenticarse contra PanAccess, en vez de autenticarse también.

        Con ``auto_extend=True`` se lanza un hilo en segundo plano que
        renueva el TTL del lock cada ``timeout/2`` segundos mientras el
        bloque ``with`` sigue corriendo -- para tareas que deben poder
        durar más de ``timeout`` sin que el lock expire solo y otra
        instancia crea que ya no está corriendo (ver ``full_sync_task``,
        que ahora puede tardar lo que haga falta).
        """
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            yield True
            return

        import threading

        from redis.lock import Lock

        # thread_local=False es necesario para que auto_extend funcione: por
        # default (thread_local=True) redis-py guarda el token del lock en
        # threading.local(), visible solo para el hilo que llamó acquire().
        # El hilo de heartbeat (_renew, más abajo) es OTRO hilo -- con el
        # default, lock.extend() ahí siempre ve local.token=None y lanza
        # LockError("Cannot extend an unlocked lock") en cada tick, atrapado
        # en silencio por el except genérico de _renew() (auditoría: el TTL
        # nunca se renovaba de verdad pese a auto_extend=True, contradiciendo
        # lo documentado en las secciones 15/19/20/24 -- una tarea que supere
        # su TTL inicial podía perder el lock a mitad de corrida y dejar que
        # otra instancia arrancara en paralelo sobre las mismas tablas).
        # thread_local=False comparte el token en un SimpleNamespace normal,
        # visible para cualquier hilo que use esta misma instancia de Lock.
        lock = Lock(cls.get_client(), key, timeout=timeout, thread_local=False)
        acquired = lock.acquire(blocking=blocking, blocking_timeout=blocking_timeout)

        stop_heartbeat = None
        heartbeat_thread = None
        if acquired and auto_extend:
            stop_heartbeat = threading.Event()
            interval = max(5, timeout // 2)

            def _renew():
                while not stop_heartbeat.wait(interval):
                    try:
                        lock.extend(timeout, replace_ttl=True)
                    except Exception:
                        logger.warning(
                            "No se pudo extender el lock '%s' (auto_extend)", key, exc_info=True
                        )

            heartbeat_thread = threading.Thread(
                target=_renew, name=f"lock-heartbeat-{key}", daemon=True
            )
            heartbeat_thread.start()

        try:
            yield acquired
        finally:
            if stop_heartbeat is not None:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=5)
            if acquired:
                try:
                    lock.release()
                except Exception:
                    pass

    @classmethod
    def validate(cls):
        if cls.PORT < 1 or cls.PORT > 65535:
            raise EnvironmentError(f"❌ REDIS_PORT inválido: {cls.PORT}")
        if cls.DB < 0 or cls.DB > 15:
            raise EnvironmentError(f"❌ REDIS_DB inválido (use 0-15): {cls.DB}")
        if cls.SSL_CERT_REQS not in ("required", "optional", "none"):
            raise EnvironmentError(
                f"❌ REDIS_SSL_CERT_REQS inválido: {cls.SSL_CERT_REQS} "
                "(use required|optional|none)"
            )


class CeleryConfig:
    TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
    TASK_TIME_LIMIT = _env_int("CELERY_TASK_TIME_LIMIT", 600)
    TASK_SOFT_TIME_LIMIT = _env_int("CELERY_TASK_SOFT_TIME_LIMIT", 540)
    WORKER_MAX_TASKS_PER_CHILD = _env_int("CELERY_WORKER_MAX_TASKS_PER_CHILD", 100)
    WORKER_POOL = RedisConfig.celery_worker_pool()

    SYNC_MINUTES = max(1, _env_int("CELERY_SYNC_MINUTES", 10))
    SMARTCARD_SYNC_MINUTES = max(1, _env_int("CELERY_SMARTCARD_SYNC_MINUTES", 10))
    # Tamaño de página para descargas de PanAccess (subscribers/smartcards/
    # products) en las tareas periódicas.
    SYNC_LIMIT = max(1, _env_int("CELERY_SYNC_LIMIT", 1000))
    SYNC_PIPELINE_QUEUE = (
        _strip_env(os.getenv("CELERY_SYNC_PIPELINE_QUEUE")) or "sync_pipeline"
    )
    FULL_SYNC_QUEUE = _strip_env(os.getenv("CELERY_FULL_SYNC_QUEUE")) or "full_sync"
    # Cola dedicada para compare_and_update_subscribers_task (reconciliación
    # completa cada pocos minutos) -- separada de sync_pipeline/full_sync
    # para poder darle su propio worker sin que compita con la sync
    # incremental ni con el full_sync nocturno.
    COMPARE_SUBSCRIBERS_QUEUE = (
        _strip_env(os.getenv("CELERY_COMPARE_SUBSCRIBERS_QUEUE")) or "compare_reconcile"
    )
    COMPARE_SUBSCRIBERS_ENABLED = _env_bool("CELERY_COMPARE_SUBSCRIBERS_ENABLED", True)
    COMPARE_SUBSCRIBERS_MINUTES = max(1, _env_int("CELERY_COMPARE_SUBSCRIBERS_MINUTES", 5))
    COMPARE_SUBSCRIBERS_LOCK_TIMEOUT = max(
        300, _env_int("CELERY_COMPARE_SUBSCRIBERS_LOCK_TIMEOUT", 1800)
    )
    # Alias legacy (HTTP / docs antiguos)
    SYNC_QUEUE = _strip_env(os.getenv("CELERY_SYNC_QUEUE")) or SYNC_PIPELINE_QUEUE
    PIPELINE_LOCK_TIMEOUT = max(
        600, _env_int("CELERY_PIPELINE_LOCK_TIMEOUT", 1800)
    )
    # TTLs de lock para las tareas de sync individuales (sync_subscribers_task,
    # sync_products_task, sync_smartcards_task, compare_and_update_smartcards_task
    # -- estas son manuales/on-demand, no todas tienen Beat schedule propio).
    # Antes 600s hardcodeado en tasks.py; se mueven acá para poder ajustarlos
    # por entorno sin tocar código. Con auto_extend=True en
    # RedisConfig.task_lock, este valor es solo el TTL inicial -- el lock se
    # renueva solo mientras la tarea sigue viva, así que no hace falta
    # sobrestimarlo "por si acaso" para cubrir el peor caso completo.
    SYNC_SUBSCRIBERS_LOCK_TIMEOUT = max(
        300, _env_int("CELERY_SYNC_SUBSCRIBERS_LOCK_TIMEOUT", 600)
    )
    SYNC_PRODUCTS_LOCK_TIMEOUT = max(
        300, _env_int("CELERY_SYNC_PRODUCTS_LOCK_TIMEOUT", 600)
    )
    SYNC_SMARTCARDS_LOCK_TIMEOUT = max(
        300, _env_int("CELERY_SYNC_SMARTCARDS_LOCK_TIMEOUT", 600)
    )
    COMPARE_SMARTCARDS_LOCK_TIMEOUT = max(
        300, _env_int("CELERY_COMPARE_SMARTCARDS_LOCK_TIMEOUT", 600)
    )
    USE_CRONTAB = _env_bool("CELERY_USE_CRONTAB", False)

    FULL_SYNC_ENABLED = _env_bool("CELERY_FULL_SYNC_ENABLED", True)
    FULL_SYNC_HOUR = max(0, min(23, _env_int("CELERY_FULL_SYNC_HOUR", 0)))
    FULL_SYNC_MINUTE = max(0, min(59, _env_int("CELERY_FULL_SYNC_MINUTE", 0)))
    FULL_SYNC_TIME_LIMIT = max(600, _env_int("CELERY_FULL_SYNC_TIME_LIMIT", 3600))
    FULL_SYNC_SOFT_TIME_LIMIT = max(540, _env_int("CELERY_FULL_SYNC_SOFT_TIME_LIMIT", 3300))
    # A pedido del cliente: full_sync_task debe poder terminar sin importar
    # cuánto tarde (el catálogo puede crecer y ya no caber en 3600s). Con
    # esto en True (default), el task NO lleva time_limit/soft_time_limit de
    # Celery -- nada la mata por tiempo. El lock distribuido y el flag
    # "full_sync_in_progress" se renuevan solos mientras la tarea sigue viva
    # (auto_extend en RedisConfig.task_lock), así que tampoco expiran antes
    # de que termine, sin importar la duración real.
    FULL_SYNC_NO_TIME_LIMIT = _env_bool("CELERY_FULL_SYNC_NO_TIME_LIMIT", True)
    # compare_and_update_all_subscribers/smartcards escalan con el tamaño
    # TOTAL del catálogo remoto (pagina todo PanAccess), no con lo que
    # cambió -- por eso corre nocturno, no cada pocos minutos. (subscribers
    # ya no precarga toda la tabla local en memoria -- solo el set de
    # códigos y, por página remota, un filter(code__in=...) puntual; ver
    # wind/functions/getSubscriber.py:compare_and_update_all_subscribers).
    # Si el worker/broker estuvo caído y el mensaje de Beat quedó encolado
    # más de FULL_SYNC_EXPIRES_SECONDS sin arrancar, Celery lo descarta en
    # vez de ejecutarlo tarde -- evita que se acumulen corridas completas
    # una detrás de otra (cada una bloquea la sync incremental mientras dura).
    FULL_SYNC_EXPIRES_SECONDS = max(
        FULL_SYNC_TIME_LIMIT, _env_int("CELERY_FULL_SYNC_EXPIRES_SECONDS", 6 * 3600)
    )

    # Reintento automático de cierres de cuenta que quedaron en
    # PENDING_CLOSURE (PanAccess falló a medias -- ver
    # wind/tasks.retry_partial_closures_task).
    CLOSURE_RETRY_ENABLED = _env_bool("CELERY_CLOSURE_RETRY_ENABLED", True)
    CLOSURE_RETRY_MINUTES = max(5, _env_int("CELERY_CLOSURE_RETRY_MINUTES", 30))
    CLOSURE_RETRY_MAX_ATTEMPTS = max(1, _env_int("CELERY_CLOSURE_RETRY_MAX_ATTEMPTS", 5))

    # Recuperación de logs de auditoría que quedaron encolados en Redis
    # (RPUSH durable, ver wind/utils/log_buffer.py) sin llegar a escribirse
    # en AuthAuditLog -- por ejemplo si el proceso se cayó entre el RPUSH y
    # el bulk_create. Corre de fondo cada pocos minutos como red de
    # seguridad; el flush normal (batch_size/flush_interval en memoria) sigue
    # siendo el camino rápido de todos los días.
    LOG_BUFFER_RECOVERY_ENABLED = _env_bool("CELERY_LOG_BUFFER_RECOVERY_ENABLED", True)
    LOG_BUFFER_RECOVERY_MINUTES = max(1, _env_int("CELERY_LOG_BUFFER_RECOVERY_MINUTES", 5))

    # Reintento automático de aprovisionamiento parcial de suscriptores
    # (contactos/license block/producto de prueba que quedaron pendientes --
    # ver wind/services/subscriber_provisioning.py y
    # wind/tasks.retry_partial_provisioning_task).
    PROVISIONING_RETRY_ENABLED = _env_bool("CELERY_PROVISIONING_RETRY_ENABLED", True)
    PROVISIONING_RETRY_MINUTES = max(5, _env_int("CELERY_PROVISIONING_RETRY_MINUTES", 15))
    PROVISIONING_RETRY_MAX_ATTEMPTS = max(1, _env_int("CELERY_PROVISIONING_RETRY_MAX_ATTEMPTS", 8))


# ---------------------------------------------------------------------------
# Caché
# ---------------------------------------------------------------------------

class CacheConfig:
    BACKEND = _strip_env(os.getenv("CACHE_BACKEND")).lower()  # locmem | vacío = redis
    USE_LOCMEM = BACKEND == "locmem"


# ---------------------------------------------------------------------------
# PanAccess API
# ---------------------------------------------------------------------------

class PanaccessConfig:
    URL = _strip_env(os.getenv("url_panaccess"))
    USERNAME = _strip_env(os.getenv("username"))
    PASSWORD = _strip_env(os.getenv("password"))
    API_TOKEN = _strip_env(os.getenv("api_token"))
    SALT = _strip_env(os.getenv("salt"))
    # Acepta hcId (canónico) o hcid (alias frecuente en .env)
    HCID = _strip_env(os.getenv("hcId")) or _strip_env(os.getenv("hcid"))
    ENCRYPTION_KEY = _strip_env(os.getenv("ENCRYPTION_KEY"))

    # Dynamic registration default product ID from .env
    REGISTRATION_PRODUCT_ID = _strip_env(os.getenv("PANACCESS_REGISTRATION_PRODUCT_ID"))

    # Nombres de operacion confirmados contra el WSDL oficial de operador
    # (https://cv01.panaccess.com/?requestMode=wsdl&v=4.3&r=operator, v4.3).
    # Los valores por defecto anteriores ("removeProductFromSmartcard",
    # singular, sin prefijo "cv") no existen como operacion documentada.
    GET_ORDERS_API = _strip_env(os.getenv("PANACCESS_GET_ORDERS_API")) or "getOrdersOfSubscriber"
    REMOVE_LICENSE_BLOCK_API = _strip_env(os.getenv("PANACCESS_REMOVE_LICENSE_BLOCK_API")) or "removeLicenseBlockFromSubscriber"
    # removeProductFromSmartcards / removeSmartcardFromOrder se probaron y no
    # hacian falta -- cleanSmartcards por si sola limpia todo, confirmado en
    # una prueba real de punta a punta (backend + PanAccess).
    CLEAN_SMARTCARDS_API = _strip_env(os.getenv("PANACCESS_CLEAN_SMARTCARDS_API")) or "cleanSmartcards"
    REMOVE_SMARTCARD_API = _strip_env(os.getenv("PANACCESS_REMOVE_SMARTCARD_API")) or "removeSmartcardFromSubscriber"
    DELETE_SUBSCRIBER_API = _strip_env(os.getenv("PANACCESS_DELETE_SUBSCRIBER_API")) or "deleteSubscriber"
    DISABLE_ORDER_API = _strip_env(os.getenv("PANACCESS_DISABLE_ORDER_API")) or "disableOrderOfSubscriber"
    REGISTRATION_TRIAL_DAYS = _env_int("REGISTRATION_TRIAL_DAYS", 30)

    # Sync incremental de suscriptores (download_subscribers_since_last):
    # tope de páginas de seguridad antes de rendirse y dejar que la
    # reconciliación periódica (compare_and_update_subscribers_task) recoja
    # lo que falte, en vez de degradar a un recorrido completo del catálogo
    # en cada corrida si el corte por 'created' nunca se alcanza.
    INCREMENTAL_SYNC_MAX_PAGES = max(1, _env_int("PANACCESS_INCREMENTAL_SYNC_MAX_PAGES", 50))
    # Margen de solapamiento (segundos) al comparar 'created' contra el
    # cursor local, para no perder registros con timestamp igual o con
    # reloj levemente desalineado respecto a PanAccess. Reprocesarlos de más
    # es inofensivo porque el guardado es upsert.
    INCREMENTAL_SYNC_OVERLAP_SECONDS = max(0, _env_int("PANACCESS_INCREMENTAL_SYNC_OVERLAP_SECONDS", 5))

    # Alias usados por wind.utils / panaccess_client (retrocompatibilidad)
    PANACCESS = URL
    KEY = ENCRYPTION_KEY

    SESSION_USE_REDIS_RAW = os.getenv("PANACCESS_SESSION_USE_REDIS")
    SESSION_TTL_SECONDS = max(300, _env_int("PANACCESS_SESSION_TTL_SECONDS", 1500))

    CIRCUIT_BREAKER_ENABLED_RAW = os.getenv("PANACCESS_CIRCUIT_BREAKER_ENABLED")
    CB_FAILURE_THRESHOLD = max(1, _env_int("PANACCESS_CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_SECONDS = max(10, _env_int("PANACCESS_CB_RECOVERY_SECONDS", 60))

    # Tamaño de lote para escrituras masivas a la BD local (bulk_create/
    # bulk_update) al guardar lo descargado de PanAccess -- subscribers,
    # smartcards y products comparten este default.
    DB_WRITE_CHUNK_SIZE = max(1, _env_int("PANACCESS_DB_WRITE_CHUNK_SIZE", 1000))

    LOGIN_INFO_TRY_LIST_API = _env_bool("PANACCESS_LOGIN_INFO_TRY_LIST_API", True)
    LOGIN_INFO_CONCURRENCY = max(1, min(_env_int("PANACCESS_LOGIN_INFO_CONCURRENCY", 10), 32))
    LOGIN_INFO_PAGE_LIMIT = _env_int("PANACCESS_LOGIN_INFO_PAGE_LIMIT", 1000)
    LOGIN_INFO_DB_CHUNK = _env_int("PANACCESS_LOGIN_INFO_DB_CHUNK", 1000)
    LOGIN_DISCOVERY_MAX_CALLS = _env_int("PANACCESS_LOGIN_DISCOVERY_MAX_CALLS", 40)

    SMARTCARD_SUBSCRIBER_MAX_PAGES = _env_int("PANACCESS_SMARTCARD_SUBSCRIBER_MAX_PAGES", 5)
    SMARTCARD_PAGE_LIMIT = _env_int("PANACCESS_SMARTCARD_PAGE_LIMIT", 1000)
    SMARTCARD_SN_CONCURRENCY = max(1, min(_env_int("PANACCESS_SMARTCARD_SN_CONCURRENCY", 5), 16))
    SMARTCARD_GLOBAL_FALLBACK = _env_bool("PANACCESS_SMARTCARD_GLOBAL_FALLBACK", False)
    SMARTCARD_SYNC_MAX_PAGES = _env_int("PANACCESS_SMARTCARD_SYNC_MAX_PAGES", 15)
    SMARTCARD_SYNC_BY_SUBSCRIBER = _env_bool("PANACCESS_SMARTCARD_SYNC_BY_SUBSCRIBER", True)
    SMARTCARD_SUBSCRIBER_CONCURRENCY = max(
        1, min(_env_int("PANACCESS_SMARTCARD_SUBSCRIBER_CONCURRENCY", 5), 32)
    )
    SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES = _env_int(
        "PANACCESS_SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES", 0
    )
    SMARTCARD_SYNC_INCREMENTAL = _env_bool("PANACCESS_SMARTCARD_SYNC_INCREMENTAL", True)
    SMARTCARD_INCREMENTAL_LOOKBACK_HOURS = max(
        1, _env_int("PANACCESS_SMARTCARD_INCREMENTAL_LOOKBACK_HOURS", 24)
    )
    # 0 = paginar hasta agotar todos los resultados del filtro (sin tope artificial)
    SMARTCARD_INCREMENTAL_MAX_PAGES = _env_int(
        "PANACCESS_SMARTCARD_INCREMENTAL_MAX_PAGES", 0
    )
    SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS = max(
        0, _env_int("PANACCESS_SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS", 24)
    )
    SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE = _env_bool(
        "PANACCESS_SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE", True
    )

    # HTTP hacia PanAccess: timeout por intento y reintentos con backoff.
    # Defaults bajados a propósito respecto al histórico (60s / 3 intentos):
    # ese peor caso podía bloquear un worker/thread hasta ~192s si PanAccess
    # estaba lento. Con los defaults de abajo el peor caso baja a ~54s
    # (2 intentos x 25s + 1 pausa de 4s), y siguen siendo configurables por
    # entorno sin tener que tocar código.
    HTTP_TIMEOUT_SECONDS = max(5, _env_int("PANACCESS_HTTP_TIMEOUT_SECONDS", 25))
    HTTP_MAX_RETRIES = max(1, min(_env_int("PANACCESS_HTTP_MAX_RETRIES", 2), 5))
    HTTP_RETRY_INITIAL_DELAY_SECONDS = max(
        1, _env_int("PANACCESS_HTTP_RETRY_INITIAL_DELAY_SECONDS", 2)
    )
    HTTP_RETRY_MAX_DELAY_SECONDS = max(
        1, _env_int("PANACCESS_HTTP_RETRY_MAX_DELAY_SECONDS", 10)
    )

    @classmethod
    def session_use_redis(cls, *, celery_eager: bool) -> bool:
        if cls.SESSION_USE_REDIS_RAW is not None:
            return _env_bool("PANACCESS_SESSION_USE_REDIS", False)
        return not celery_eager

    @classmethod
    def circuit_breaker_enabled(cls, *, debug: bool) -> bool:
        if cls.CIRCUIT_BREAKER_ENABLED_RAW is not None:
            return _env_bool("PANACCESS_CIRCUIT_BREAKER_ENABLED", False)
        return not debug

    @classmethod
    def validate(cls, *, debug: bool | None = None):
        # `debug` es opcional a propósito: casi todos los call sites de este
        # método (wind/utils/panaccess_auth.py, encryption.py,
        # panaccess_client.py, create_subscriber.py) lo llaman sin
        # argumentos. Si por eso "debug" cayera siempre en False, un
        # entorno de desarrollo real con DEBUG=true en el .env seguiría
        # bloqueado por el check de HTTPS de más abajo en cualquier llamada
        # que no sea la de settings.py -- así que si no se pasa
        # explícitamente, se resuelve del mismo DEBUG del entorno
        # (DjangoConfig no depende de Django estar inicializado, solo lee
        # la variable de entorno).
        effective_debug = DjangoConfig.DEBUG if debug is None else debug

        missing = []
        if not cls.URL:
            missing.append("url_panaccess")
        if not cls.USERNAME:
            missing.append("username")
        if not cls.PASSWORD:
            missing.append("password")
        if not cls.API_TOKEN:
            missing.append("api_token")
        if not cls.SALT:
            missing.append("salt")
        if not cls.HCID:
            missing.append("hcId")
        if not cls.ENCRYPTION_KEY:
            missing.append("ENCRYPTION_KEY")
        if missing:
            raise EnvironmentError(f"❌ Faltan variables de entorno: {', '.join(missing)}")

        # url_panaccess sin HTTPS significa que el password de la cuenta de
        # servicio (hasheado con MD5+salt fijo, ver wind/utils/panaccess_auth.py)
        # y el sessionId viajan en texto plano, interceptables por cualquiera
        # en la red intermedia. Falla el arranque en vez de permitirlo
        # silenciosamente -- salvo que se declare explícitamente un entorno
        # de desarrollo/pruebas (DEBUG=true) o se fuerce con
        # PANACCESS_ALLOW_INSECURE_HTTP=true.
        if cls.URL and not cls.URL.lower().startswith("https://"):
            allow_insecure = _env_bool("PANACCESS_ALLOW_INSECURE_HTTP", False)
            if effective_debug or allow_insecure:
                logger.warning(
                    "url_panaccess no usa HTTPS (%s). Permitido solo por DEBUG=true "
                    "o PANACCESS_ALLOW_INSECURE_HTTP=true -- nunca dejar así en "
                    "producción real.",
                    cls.URL,
                )
            else:
                raise EnvironmentError(
                    "❌ url_panaccess debe usar HTTPS en producción (valor actual: "
                    f"{cls.URL}). Si es un entorno de desarrollo/pruebas, active "
                    "DEBUG=true o PANACCESS_ALLOW_INSECURE_HTTP=true explícitamente."
                )


# ---------------------------------------------------------------------------
# JWT / email / throttling
# ---------------------------------------------------------------------------

class JwtConfig:
    USE_COOKIES = _env_bool("JWT_USE_COOKIES", False)
    AUTH_COOKIE = _strip_env(os.getenv("JWT_AUTH_COOKIE")) or "wind-auth"
    REFRESH_COOKIE = _strip_env(os.getenv("JWT_AUTH_REFRESH_COOKIE")) or "wind-refresh-token"
    ACCESS_MINUTES_DEV = _env_int("JWT_ACCESS_MINUTES", 60)
    REFRESH_DAYS = 7

    @classmethod
    def access_minutes(cls, *, debug: bool) -> int:
        if os.getenv("JWT_ACCESS_MINUTES") is not None:
            return max(1, _env_int("JWT_ACCESS_MINUTES", 15))
        return 60 if debug else 15


class EmailConfig:
    BACKEND = _strip_env(os.getenv("EMAIL_BACKEND"))
    HOST = _strip_env(os.getenv("EMAIL_HOST"))
    PORT = _env_int("EMAIL_PORT", 587)
    HOST_USER = _strip_env(os.getenv("EMAIL_HOST_USER"))
    HOST_PASSWORD = _strip_env(os.getenv("EMAIL_HOST_PASSWORD"))
    USE_TLS = _env_bool("EMAIL_USE_TLS", True)
    DEFAULT_FROM = _strip_env(os.getenv("DEFAULT_FROM_EMAIL"))

    WELCOME_SUBJECT = (
        _strip_env(os.getenv("EMAIL_WELCOME_SUBJECT"))
        or "Bienvenido a WindTV — tus datos de acceso"
    )
    SUPPORT_ADDRESS = _strip_env(os.getenv("EMAIL_SUPPORT_ADDRESS")) or "info@wind.do"
    # A dónde avisar cuando un cierre de cuenta agota los reintentos
    # automáticos (retry_partial_closures_task). Por defecto, el mismo
    # correo de soporte.
    OPS_ALERT_ADDRESS = _strip_env(os.getenv("EMAIL_OPS_ALERT_ADDRESS")) or SUPPORT_ADDRESS
    SUPPORT_PHONE = _strip_env(os.getenv("EMAIL_SUPPORT_PHONE")) or "809.200.3000"
    TERMS_URL = _strip_env(os.getenv("EMAIL_TERMS_URL")) or ""
    GOOGLE_PLAY_URL = _strip_env(os.getenv("WIND_APP_GOOGLE_PLAY_URL")) or ""
    APP_STORE_URL = _strip_env(os.getenv("WIND_APP_APP_STORE_URL")) or ""
    SOCIAL_PASSWORD_MESSAGE = "Cuenta social no usa contraseña."

    @classmethod
    def account_verification(cls, *, debug: bool) -> str:
        raw = _strip_env(os.getenv("ACCOUNT_EMAIL_VERIFICATION"))
        if raw:
            v = raw.lower()
            if v not in ("none", "optional", "mandatory"):
                raise EnvironmentError(
                    "ACCOUNT_EMAIL_VERIFICATION inválido. Use: none|optional|mandatory"
                )
            return v
        return "none" if debug else "mandatory"

    @classmethod
    def resolved_backend(cls, *, debug: bool) -> str:
        if cls.BACKEND:
            return cls.BACKEND
        if not debug and cls.HOST:
            return "django.core.mail.backends.smtp.EmailBackend"
        return "django.core.mail.backends.console.EmailBackend"


class ThrottleConfig:
    ANON = _strip_env(os.getenv("DRF_THROTTLE_ANON")) or "60/minute"
    USER = _strip_env(os.getenv("DRF_THROTTLE_USER")) or "600/minute"
    PROFILE = _strip_env(os.getenv("DRF_THROTTLE_PROFILE")) or "120/minute"
    SYNC_ADMIN = _strip_env(os.getenv("DRF_THROTTLE_SYNC_ADMIN")) or "30/minute"
    REGISTER = _strip_env(os.getenv("DRF_THROTTLE_REGISTER")) or "10/hour"
    PASSWORD_RESET = _strip_env(os.getenv("DRF_THROTTLE_PASSWORD_RESET")) or "5/hour"
    # Login social (Google/Facebook): antes sin scope propio, caía en el
    # límite genérico anónimo (60/minute) -- mucho más permisivo que el resto
    # de las acciones sensibles de auth. No tan estricto como register/
    # password_reset porque es una acción legítima de uso frecuente (ver
    # auditoría).
    SOCIAL_LOGIN = _strip_env(os.getenv("DRF_THROTTLE_SOCIAL_LOGIN")) or "20/minute"
    # Listar/revocar dispositivos vinculados (Fase 3): antes sin scope
    # propio, caía en el límite genérico de usuario (600/minute) -- pensado
    # para navegación normal, no para una acción de escritura que además
    # dispara un broadcast por WebSocket en cada llamada (segunda auditoría).
    DEVICE_SESSION = _strip_env(os.getenv("DRF_THROTTLE_DEVICE_SESSION")) or "60/minute"


# ---------------------------------------------------------------------------
# Login social
# ---------------------------------------------------------------------------

class SocialConfig:
    _PROVIDERS_ENV = os.getenv("SOCIAL_LOGIN_PROVIDERS")
    PROVIDERS_RAW = _strip_env(_PROVIDERS_ENV) if _PROVIDERS_ENV is not None else None

    GOOGLE_CLIENT_ID = _strip_env(os.getenv("GOOGLE_CLIENT_ID"))
    GOOGLE_CLIENT_SECRET = _strip_env(os.getenv("GOOGLE_CLIENT_SECRET"))
    GOOGLE_REDIRECT_URI = _strip_env(os.getenv("GOOGLE_REDIRECT_URI"))

    FACEBOOK_APP_ID = _strip_env(os.getenv("FACEBOOK_APP_ID"))
    FACEBOOK_APP_SECRET = _strip_env(os.getenv("FACEBOOK_APP_SECRET"))
    FACEBOOK_REDIRECT_URI = _strip_env(os.getenv("FACEBOOK_REDIRECT_URI"))

    APPLE_CLIENT_ID = _strip_env(os.getenv("APPLE_CLIENT_ID"))
    APPLE_CLIENT_SECRET = _strip_env(os.getenv("APPLE_CLIENT_SECRET"))
    APPLE_REDIRECT_URI = _strip_env(os.getenv("APPLE_REDIRECT_URI"))

    _PROVIDER_ENV = {
        "google": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"),
        "facebook": ("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET", "FACEBOOK_REDIRECT_URI"),
        "apple": ("APPLE_CLIENT_ID", "APPLE_CLIENT_SECRET", "APPLE_REDIRECT_URI"),
    }

    @classmethod
    def enabled_providers(cls) -> list[str]:
        if cls._PROVIDERS_ENV is None:
            raw = "google,facebook"
        else:
            raw = cls._PROVIDERS_ENV
        if not raw.strip():
            return []
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    @classmethod
    def validate(cls):
        providers = cls.enabled_providers()
        if not providers:
            return

        missing = []
        unknown = []
        for provider in providers:
            env_names = cls._PROVIDER_ENV.get(provider)
            if not env_names:
                unknown.append(provider)
                continue
            for env_name in env_names:
                if not _strip_env(os.getenv(env_name)):
                    missing.append(env_name)

        if unknown:
            raise EnvironmentError(
                f"SOCIAL_LOGIN_PROVIDERS inválido: {', '.join(unknown)}. "
                f"Válidos: {', '.join(cls._PROVIDER_ENV)}"
            )
        if missing:
            raise EnvironmentError(
                f"Faltan variables para login social ({', '.join(providers)}): "
                f"{', '.join(missing)}"
            )


# ---------------------------------------------------------------------------
# Features / flags operativos
# ---------------------------------------------------------------------------

class FeatureConfig:
    SYNC_HTTP_ASYNC = _env_bool("SYNC_HTTP_ASYNC", True)
    FULL_SYNC_HTTP_ENABLED = _env_bool("FULL_SYNC_HTTP_ENABLED", False)
    PANACCESS_OPS_HTTP_ENABLED = _env_bool("PANACCESS_OPS_HTTP_ENABLED", False)
    CREATE_SUBSCRIBER_PUBLIC_ENABLED = _env_bool("CREATE_SUBSCRIBER_PUBLIC_ENABLED", True)
    # Por defecto OFF: el registro público sigue encadenando sync las 6-9
    # llamadas a PanAccess (comportamiento actual, sin cambios). Si se activa,
    # create_subscriber_view solo hace addSubscriber sync y devuelve de una
    # vez; el resto (contactos, license block, producto/trial, búsqueda de
    # smartcards) corre en background (finish_subscriber_provisioning_task).
    # OJO: en modo async la respuesta YA NO incluye "token"/"credentials_url"/
    # "license_block_added"/"contacts_added"/"assigned_smartcards" de forma
    # síncrona -- coordinar con el equipo de frontend antes de activarlo.
    CREATE_SUBSCRIBER_ASYNC_ENRICHMENT = _env_bool("CREATE_SUBSCRIBER_ASYNC_ENRICHMENT", False)
    CLOSE_SUBSCRIBER_HTTP_ENABLED = _env_bool("CLOSE_SUBSCRIBER_HTTP_ENABLED", False)
    CLOSE_SUBSCRIBER_DASHBOARD_ENABLED = _env_bool("CLOSE_SUBSCRIBER_DASHBOARD_ENABLED", True)


# ---------------------------------------------------------------------------
# Estáticos / observabilidad
# ---------------------------------------------------------------------------

class StaticConfig:
    CDN_URL = _strip_env(os.getenv("CDN_STATIC_URL"))


class RecaptchaConfig:
    """
    reCAPTCHA v3 en /wind/create-subscriber/ (registro público) -- mitigación
    contra bots/scripts/IA creando abonados masivamente (ver auditoría).

    Opt-in a propósito: si RECAPTCHA_SECRET_KEY no está configurado, la
    verificación se omite (no rompe clientes web/móvil existentes que
    todavía no integran el widget/token). Configurar la variable de entorno
    una vez el frontend envíe `recaptcha_token` en el body del registro para
    empezar a exigirlo.
    """

    SECRET_KEY = _strip_env(os.getenv("RECAPTCHA_SECRET_KEY"))
    MIN_SCORE = float(os.getenv("RECAPTCHA_MIN_SCORE", "0.5"))


class CrmIntegrationConfig:
    """
    Secreto compartido para la integración M2M del bot de CRM del cliente
    (ver auditoría): `validate_subscriber_email_view` estaba detrás de
    AllowAny y permitía enumerar emails registrados a cualquiera. La
    funcionalidad es legítima -- el cliente tiene un bot interno que
    pre-valida el email antes de lanzar el flujo completo de alta en
    PanAccess, para no reiniciar todo el proceso cuando PanAccess rechaza
    un email duplicado a mitad de camino -- pero el endpoint no distinguía
    a ese bot de cualquier visitante anónimo. Como es una integración
    máquina-a-máquina (no un formulario con humano detrás), la mitigación
    correcta es un API key compartido en un header, no reCAPTCHA.

    Sin CRM_EMAIL_CHECK_API_KEY configurado, el endpoint se deniega por
    completo (fail-closed) en vez de quedar abierto igual que antes --
    configúrelo acá y entréguele el mismo valor al equipo del bot antes de
    desplegar este cambio.
    """

    EMAIL_CHECK_API_KEY = _strip_env(os.getenv("CRM_EMAIL_CHECK_API_KEY"))


class TrustedProxyConfig:
    """
    Proxies de confianza desde los que sí se acepta `X-Forwarded-For` como
    IP real del cliente (ver auditoría: bypass de IP allowlist falseando
    este header). Por defecto solo localhost, coherente con que
    Daphne/Django solo escuchan en 127.0.0.1 detrás de nginx (ver
    deploy/systemd/*.service) -- si REMOTE_ADDR no está en esta lista, el
    header se ignora por completo y se usa REMOTE_ADDR tal cual.

    Usado hoy por `wind/utils/websocket_utils.get_client_ip()` (superficie
    de pareo UDID). El mismo problema en `sync_admin_ip_middleware.py`
    sigue pendiente de una decisión de deploy más amplia (nginx +
    variable), ver auditoría -- esta config queda lista para reutilizarse
    ahí también cuando se implemente esa parte.
    """

    TRUSTED_PROXIES = _csv("TRUSTED_PROXIES") or ["127.0.0.1", "::1"]


class HealthCheckConfig:
    """
    Token opcional para habilitar el check profundo (PanAccess) en /health/.

    Sin este token configurado, /health/ solo reporta DB + caché (liveness
    liviano) -- no fuerza un login real contra PanAccess (que cuenta contra
    su límite de intentos) ni expone el texto de sus errores públicamente.
    Configurar HEALTH_CHECK_TOKEN acá y en el monitoreo interno (enviado en
    el header 'X-Health-Token') para habilitar el check completo solo para
    quien tenga el secreto.
    """

    TOKEN = _strip_env(os.getenv("HEALTH_CHECK_TOKEN"))


class SentryConfig:
    DSN = _strip_env(os.getenv("SENTRY_DSN"))
    ENVIRONMENT = _strip_env(os.getenv("SENTRY_ENVIRONMENT"))
    # _env_float en vez de float(os.getenv(...)) a secas -- antes, un valor
    # no numérico en SENTRY_TRACES_SAMPLE_RATE (typo, valor vacío, etc.)
    # tumbaba el arranque completo de la aplicación con un ValueError al
    # importar este módulo, por una variable que ni siquiera es crítica
    # (solo afecta el muestreo de trazas de Sentry). Ahora loguea un
    # warning y sigue con el default.
    TRACES_SAMPLE_RATE = _env_float("SENTRY_TRACES_SAMPLE_RATE", 0.1)

    @classmethod
    def environment(cls, *, debug: bool) -> str:
        if cls.ENVIRONMENT:
            return cls.ENVIRONMENT
        return "development" if debug else "production"


# ---------------------------------------------------------------------------
# Pruebas de carga (scripts/load/locustfile.py)
# ---------------------------------------------------------------------------

class LocustConfig:
    USERNAME = _strip_env(os.getenv("LOCUST_USERNAME"))
    PASSWORD = _strip_env(os.getenv("LOCUST_PASSWORD"))
