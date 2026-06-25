"""
Configuración centralizada desde variables de entorno (.env).
Todas las variables configurables del proyecto deben declararse aquí.
settings.py y el resto del código importan desde estas clases.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(override=True)
load_dotenv()


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


# ---------------------------------------------------------------------------
# Django / seguridad HTTP
# ---------------------------------------------------------------------------

class DjangoConfig:
    SECRET_KEY = _strip_env(os.getenv("SECRET_KEY"))
    DEBUG = _env_bool("DEBUG", False)
    ALLOWED_HOSTS = [_normalize_host(h) for h in _csv("ALLOWED_HOSTS")]
    PRODUCTION_HTTPS = _env_bool("PRODUCTION_HTTPS", False)
    SYNC_ADMIN_IP_ALLOWLIST = _csv("SYNC_ADMIN_IP_ALLOWLIST")

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
    CONN_MAX_AGE = _env_int("DB_CONN_MAX_AGE", 0)

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

    REDIS_URL = _strip_env(os.getenv("REDIS_URL"))
    CELERY_BROKER_URL = _strip_env(os.getenv("CELERY_BROKER_URL"))
    CELERY_RESULT_BACKEND = _strip_env(os.getenv("CELERY_RESULT_BACKEND"))

    @classmethod
    def build_url(cls, db: int | None = None) -> str:
        database = cls.DB if db is None else db
        auth = f":{quote(cls.PASSWORD, safe='')}@" if cls.PASSWORD else ""
        return f"redis://{auth}{cls.HOST}:{cls.PORT}/{database}"

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
    def task_lock(cls, key: str, *, timeout: int = 600):
        from django.conf import settings

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            yield True
            return

        from redis.lock import Lock

        lock = Lock(cls.get_client(), key, timeout=timeout)
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
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


class CeleryConfig:
    TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
    TASK_TIME_LIMIT = _env_int("CELERY_TASK_TIME_LIMIT", 600)
    TASK_SOFT_TIME_LIMIT = _env_int("CELERY_TASK_SOFT_TIME_LIMIT", 540)
    WORKER_MAX_TASKS_PER_CHILD = _env_int("CELERY_WORKER_MAX_TASKS_PER_CHILD", 100)
    WORKER_POOL = RedisConfig.celery_worker_pool()

    SYNC_MINUTES = max(1, _env_int("CELERY_SYNC_MINUTES", 10))
    SMARTCARD_SYNC_MINUTES = max(1, _env_int("CELERY_SMARTCARD_SYNC_MINUTES", 10))
    SYNC_LIMIT = max(1, _env_int("CELERY_SYNC_LIMIT", 200))
    SYNC_PIPELINE_QUEUE = (
        _strip_env(os.getenv("CELERY_SYNC_PIPELINE_QUEUE")) or "sync_pipeline"
    )
    FULL_SYNC_QUEUE = _strip_env(os.getenv("CELERY_FULL_SYNC_QUEUE")) or "full_sync"
    # Alias legacy (HTTP / docs antiguos)
    SYNC_QUEUE = _strip_env(os.getenv("CELERY_SYNC_QUEUE")) or SYNC_PIPELINE_QUEUE
    PIPELINE_LOCK_TIMEOUT = max(
        600, _env_int("CELERY_PIPELINE_LOCK_TIMEOUT", 1800)
    )
    USE_CRONTAB = _env_bool("CELERY_USE_CRONTAB", False)

    FULL_SYNC_ENABLED = _env_bool("CELERY_FULL_SYNC_ENABLED", True)
    FULL_SYNC_HOUR = max(0, min(23, _env_int("CELERY_FULL_SYNC_HOUR", 0)))
    FULL_SYNC_MINUTE = max(0, min(59, _env_int("CELERY_FULL_SYNC_MINUTE", 0)))
    FULL_SYNC_TIME_LIMIT = max(600, _env_int("CELERY_FULL_SYNC_TIME_LIMIT", 3600))
    FULL_SYNC_SOFT_TIME_LIMIT = max(540, _env_int("CELERY_FULL_SYNC_SOFT_TIME_LIMIT", 3300))


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

    # Alias usados por wind.utils / panaccess_client (retrocompatibilidad)
    PANACCESS = URL
    KEY = ENCRYPTION_KEY

    SESSION_USE_REDIS_RAW = os.getenv("PANACCESS_SESSION_USE_REDIS")
    SESSION_TTL_SECONDS = max(300, _env_int("PANACCESS_SESSION_TTL_SECONDS", 1500))

    CIRCUIT_BREAKER_ENABLED_RAW = os.getenv("PANACCESS_CIRCUIT_BREAKER_ENABLED")
    CB_FAILURE_THRESHOLD = max(1, _env_int("PANACCESS_CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_SECONDS = max(10, _env_int("PANACCESS_CB_RECOVERY_SECONDS", 60))

    LOGIN_INFO_TRY_LIST_API = _env_bool("PANACCESS_LOGIN_INFO_TRY_LIST_API", True)
    LOGIN_INFO_CONCURRENCY = max(1, min(_env_int("PANACCESS_LOGIN_INFO_CONCURRENCY", 10), 32))
    LOGIN_INFO_PAGE_LIMIT = _env_int("PANACCESS_LOGIN_INFO_PAGE_LIMIT", 200)
    LOGIN_INFO_DB_CHUNK = _env_int("PANACCESS_LOGIN_INFO_DB_CHUNK", 200)
    LOGIN_DISCOVERY_MAX_CALLS = _env_int("PANACCESS_LOGIN_DISCOVERY_MAX_CALLS", 40)

    SMARTCARD_SUBSCRIBER_MAX_PAGES = _env_int("PANACCESS_SMARTCARD_SUBSCRIBER_MAX_PAGES", 5)
    SMARTCARD_PAGE_LIMIT = _env_int("PANACCESS_SMARTCARD_PAGE_LIMIT", 100)
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
    def validate(cls):
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


# ---------------------------------------------------------------------------
# Estáticos / observabilidad
# ---------------------------------------------------------------------------

class StaticConfig:
    CDN_URL = _strip_env(os.getenv("CDN_STATIC_URL"))


class SentryConfig:
    DSN = _strip_env(os.getenv("SENTRY_DSN"))
    ENVIRONMENT = _strip_env(os.getenv("SENTRY_ENVIRONMENT"))
    TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))

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
