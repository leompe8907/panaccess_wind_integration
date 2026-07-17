"""
Django settings for panaccess_wind_integration project.
"""
import sys
from pathlib import Path
from celery.schedules import crontab, timedelta
from dotenv import load_dotenv
from appConfig import (
    CacheConfig,
    CeleryConfig,
    CorsConfig,
    DatabaseConfig,
    DjangoConfig,
    EmailConfig,
    JwtConfig,
    PanaccessConfig,
    RedisConfig,
    SentryConfig,
    SocialConfig,
    StaticConfig,
    ThrottleConfig,
)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Cargar variables de entorno desde .env (si existe)
load_dotenv(BASE_DIR / '.env')

# Configurar encoding UTF-8 para Windows
if sys.platform == 'win32':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Validar las configuraciones
DjangoConfig.validate()
SocialConfig.validate()
PanaccessConfig.validate()
if DatabaseConfig.use_postgresql():
    DatabaseConfig.configure()
RedisConfig.validate()
CorsConfig.validate_no_allow_all()


# Security settings
SECRET_KEY = DjangoConfig.SECRET_KEY
DEBUG = DjangoConfig.DEBUG
ALLOWED_HOSTS = DjangoConfig.ALLOWED_HOSTS or ['localhost', '127.0.0.1']

PRODUCTION_HTTPS = DjangoConfig.production_https(debug=DEBUG)
if PRODUCTION_HTTPS and not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
elif DjangoConfig.production_https_explicitly_disabled(debug=DEBUG):
    # DEBUG=False y alguien puso PRODUCTION_HTTPS=false a propósito: se deja
    # constancia bien visible, porque HSTS/cookies seguras/redirect SSL
    # quedan desactivados y antes esto podía pasar desapercibido.
    import logging as _logging

    _logging.getLogger("wind").warning(
        "PRODUCTION_HTTPS=false con DEBUG=false: HSTS, cookies seguras y "
        "redirect SSL están DESACTIVADOS. Confirma que es intencional "
        "(p. ej. TLS terminado en un balanceador externo con su propia "
        "política de seguridad)."
    )

SYNC_ADMIN_IP_ALLOWLIST = DjangoConfig.SYNC_ADMIN_IP_ALLOWLIST


# ============================================================================
# APLICACIONES INSTALADAS
# ============================================================================
INSTALLED_APPS = [
    'daphne',  # Daphne debe estar al inicio para interceptar runserver para ASGI/WebSockets
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'wind',
    'channels',  # Django Channels
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.facebook',
    'allauth.socialaccount.providers.google',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'dj_rest_auth',
    'dj_rest_auth.registration',
]

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

# ============================================================================
# MIDDLEWARE
# ============================================================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

if SYNC_ADMIN_IP_ALLOWLIST:
    MIDDLEWARE.insert(
        1,
        "wind.middleware.sync_admin_ip_middleware.SyncAdminIPRestrictionMiddleware",
    )

SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin-allow-popups'

# ============================================================================
# CORS
# ============================================================================
CORS_ALLOWED_ORIGINS = CorsConfig.resolved_origins(debug=DEBUG)
CORS_ALLOW_CREDENTIALS = CorsConfig.ALLOW_CREDENTIALS

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# ============================================================================
# CONFIGURACIÓN DE REST FRAMEWORK
# ============================================================================
REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # Igual que JWTAuthentication, pero además rechaza access tokens
        # emitidos antes del último cambio de contraseña del usuario (ver
        # wind/services/jwt_invalidation.py) -- el blacklist de simplejwt
        # solo cubre refresh tokens ya rotados.
        'wind.services.jwt_invalidation.PasswordAwareJWTAuthentication',
    ),
    'DEFAULT_THROTTLE_CLASSES': [
        'wind.throttles.AnonBurstThrottle',
        'wind.throttles.UserBurstThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': ThrottleConfig.ANON,
        'user': ThrottleConfig.USER,
        'profile': ThrottleConfig.PROFILE,
        'sync_admin': ThrottleConfig.SYNC_ADMIN,
        'register': ThrottleConfig.REGISTER,
        'password_reset': ThrottleConfig.PASSWORD_RESET,
    },
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# ============================================================================
# JWT y DJ-REST-AUTH
# ============================================================================
REST_AUTH = {
    'USE_JWT': True,
    'USER_DETAILS_SERIALIZER': 'wind.serializers.JWTUserDetailsSerializer',
    'LOGIN_SERIALIZER': 'wind.auth_serializers.PanAccessLoginSerializer',
}

if JwtConfig.USE_COOKIES:
    REST_AUTH.update(
        {
            "JWT_AUTH_COOKIE": JwtConfig.AUTH_COOKIE,
            "JWT_AUTH_REFRESH_COOKIE": JwtConfig.REFRESH_COOKIE,
        }
    )

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(
        minutes=max(1, JwtConfig.access_minutes(debug=DEBUG)),
    ),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=JwtConfig.REFRESH_DAYS),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ============================================================================
# ALLAUTH: Autenticación y flujos sociales
# ============================================================================
SITE_ID = 1
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']

# ============================================================================
# EMAIL / ALLAUTH: verificación de email
# ============================================================================
ACCOUNT_EMAIL_VERIFICATION = EmailConfig.account_verification(debug=DEBUG)
EMAIL_BACKEND = EmailConfig.resolved_backend(debug=DEBUG)
EMAIL_HOST = EmailConfig.HOST
EMAIL_PORT = EmailConfig.PORT
EMAIL_HOST_USER = EmailConfig.HOST_USER
EMAIL_HOST_PASSWORD = EmailConfig.HOST_PASSWORD
EMAIL_USE_TLS = EmailConfig.USE_TLS
DEFAULT_FROM_EMAIL = EmailConfig.DEFAULT_FROM

# Adaptador personalizado para conectar Google con PanAccess
SOCIALACCOUNT_ADAPTER = 'wind.adapters.PanAccessSocialAccountAdapter'
# El proveedor social (Google/Facebook) ya verifica el email.
SOCIALACCOUNT_EMAIL_VERIFICATION = 'none'


# ============================================================================
# URLs & WSGI & ASGI
# ============================================================================
ROOT_URLCONF = 'panaccess_wind_integration.urls'
WSGI_APPLICATION = 'panaccess_wind_integration.wsgi.application'
ASGI_APPLICATION = 'panaccess_wind_integration.asgi.application'


# ============================================================================
# CHANNELS (WebSockets)
# ============================================================================
if 'test' in sys.argv:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                # Usar db 2 para evitar colisión con caché o Celery Beat
                "hosts": [RedisConfig.build_url(db=2)],
            },
        },
    }


# ============================================================================
# TEMPLATES
# ============================================================================
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


# ============================================================================
# BASE DE DATOS
# ============================================================================
if DatabaseConfig.use_postgresql():
    DATABASES = {'default': DatabaseConfig.django_default_database()}
    _replica = DatabaseConfig.django_replica_database()
    if _replica:
        DATABASES['replica'] = _replica
        DATABASE_ROUTERS = ['wind.db_router.PrimaryReplicaRouter']
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'es'
TIME_ZONE = 'America/Santo_Domingo'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = ((StaticConfig.CDN_URL or '/static/').rstrip('/') + '/')
STATIC_ROOT = BASE_DIR / 'staticfiles'

if DEBUG:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    WHITENOISE_USE_FINDERS = True
else:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ============================================================================
# CELERY / REDIS
# ============================================================================
REDIS_HOST = RedisConfig.HOST
REDIS_PORT = RedisConfig.PORT
REDIS_DB = RedisConfig.DB
REDIS_PASSWORD = RedisConfig.PASSWORD

CELERY_BROKER_URL = RedisConfig.broker_url()
CELERY_RESULT_BACKEND = RedisConfig.result_backend_url()
CELERY_TASK_ALWAYS_EAGER = RedisConfig.celery_eager()
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER
CELERY_TASK_TIME_LIMIT = CeleryConfig.TASK_TIME_LIMIT
CELERY_TASK_SOFT_TIME_LIMIT = CeleryConfig.TASK_SOFT_TIME_LIMIT
CELERY_WORKER_MAX_TASKS_PER_CHILD = CeleryConfig.WORKER_MAX_TASKS_PER_CHILD
CELERY_WORKER_POOL = CeleryConfig.WORKER_POOL
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = not CELERY_TASK_ALWAYS_EAGER
CELERY_ENABLE_UTC = True
CELERY_TIMEZONE = TIME_ZONE

# Colas: pipeline incremental (serie), full_sync (nocturno, exclusivo), y
# compare_reconcile (reconciliación completa de subscribers cada pocos
# minutos -- cola propia para poder darle un worker dedicado, ver abajo).
_PIPELINE_QUEUE = CeleryConfig.SYNC_PIPELINE_QUEUE
_FULL_SYNC_QUEUE = CeleryConfig.FULL_SYNC_QUEUE
_SYNC_QUEUE = CeleryConfig.SYNC_QUEUE  # alias legacy
_COMPARE_SUBSCRIBERS_QUEUE = CeleryConfig.COMPARE_SUBSCRIBERS_QUEUE

CELERY_TASK_ROUTES = {
    'wind.tasks.periodic_sync_pipeline_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.sync_subscribers_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.compare_and_update_subscribers_task': {'queue': _COMPARE_SUBSCRIBERS_QUEUE},
    'wind.tasks.sync_smartcards_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.compare_and_update_smartcards_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.sync_products_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.full_sync_task': {'queue': _FULL_SYNC_QUEUE},
    # Refresh puntual de un suscriptor (disparado desde GET de perfil,
    # subscriber_catalog.py) -- va a la misma cola liviana del pipeline
    # incremental, no necesita cola dedicada.
    'wind.tasks.refresh_subscriber_profile_task': {'queue': _PIPELINE_QUEUE},
    # Sin ruta explícita, estas caían en la cola default de Celery ("celery")
    # -- que en este deploy NO tiene ningún worker escuchándola (ver
    # docs/DEPLOYMENT_UBUNTU_NATIVE.md y auditoría, sección 20). Se rutean a
    # sync_pipeline (worker liviano que ya existe) para que efectivamente se
    # ejecuten en vez de acumularse sin procesar.
    'wind.tasks.finish_subscriber_provisioning_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.send_welcome_credentials_email_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.send_password_reset_email_task': {'queue': _PIPELINE_QUEUE},
    'wind.tasks.send_verification_email_task': {'queue': _PIPELINE_QUEUE},
}

_SYNC_MINUTES = CeleryConfig.SYNC_MINUTES
_SYNC_LIMIT = CeleryConfig.SYNC_LIMIT
_PIPELINE_LOCK_TIMEOUT = CeleryConfig.PIPELINE_LOCK_TIMEOUT
_SMARTCARD_SYNC_MINUTES = CeleryConfig.SMARTCARD_SYNC_MINUTES

if CeleryConfig.USE_CRONTAB:
    _SCHEDULE = crontab(minute=f"*/{_SYNC_MINUTES}")
    _SMARTCARD_SCHEDULE = crontab(minute=f"*/{_SMARTCARD_SYNC_MINUTES}")
else:
    _SCHEDULE = timedelta(minutes=_SYNC_MINUTES)
    _SMARTCARD_SCHEDULE = timedelta(minutes=_SMARTCARD_SYNC_MINUTES)

_FULL_SYNC_HOUR = CeleryConfig.FULL_SYNC_HOUR
_FULL_SYNC_MINUTE = CeleryConfig.FULL_SYNC_MINUTE
_FULL_SYNC_TIME_LIMIT = CeleryConfig.FULL_SYNC_TIME_LIMIT
_FULL_SYNC_SOFT_LIMIT = CeleryConfig.FULL_SYNC_SOFT_TIME_LIMIT
_FULL_SYNC_ENABLED = CeleryConfig.FULL_SYNC_ENABLED

CELERY_BEAT_SCHEDULE = {
    "periodic-sync-pipeline": {
        "task": "wind.tasks.periodic_sync_pipeline_task",
        "schedule": _SCHEDULE,
        "options": {
            "queue": _PIPELINE_QUEUE,
            "soft_time_limit": _PIPELINE_LOCK_TIMEOUT,
            "time_limit": _PIPELINE_LOCK_TIMEOUT + 60,
            # Si un mensaje quedó encolado más de un intervalo sin arrancar,
            # se descarta en vez de correr atrasado y encimarse con el
            # siguiente disparo de Beat.
            "expires": _SYNC_MINUTES * 60,
        },
        "args": (_SYNC_LIMIT,),
    },
}

if _FULL_SYNC_ENABLED:
    _full_sync_options = {
        "queue": _FULL_SYNC_QUEUE,
        # Si el mensaje quedó encolado más de esto sin arrancar (broker/
        # worker caído esa noche), se descarta en vez de correr tarde y
        # posiblemente solaparse con la corrida del día siguiente. Esto es
        # sobre el tiempo en cola SIN ARRANCAR -- no limita cuánto puede
        # durar la tarea una vez que ya arrancó.
        "expires": CeleryConfig.FULL_SYNC_EXPIRES_SECONDS,
    }
    if not CeleryConfig.FULL_SYNC_NO_TIME_LIMIT:
        # Comportamiento anterior: límites duros de Celery. Con el default
        # (FULL_SYNC_NO_TIME_LIMIT=true) se omiten a propósito -- ver
        # wind/tasks.py:full_sync_task.
        _full_sync_options["soft_time_limit"] = _FULL_SYNC_SOFT_LIMIT
        _full_sync_options["time_limit"] = _FULL_SYNC_TIME_LIMIT

    CELERY_BEAT_SCHEDULE["full-sync-nightly"] = {
        "task": "wind.tasks.full_sync_task",
        "schedule": crontab(hour=_FULL_SYNC_HOUR, minute=_FULL_SYNC_MINUTE),
        "options": _full_sync_options,
        "kwargs": {"limit": _SYNC_LIMIT},
    }

if CeleryConfig.COMPARE_SUBSCRIBERS_ENABLED:
    # Reconciliación completa de subscribers (compara TODO el catálogo local
    # contra TODO el remoto -- puede crear, actualizar o borrar filas) cada
    # CELERY_COMPARE_SUBSCRIBERS_MINUTES (default 5). A diferencia del resto
    # de tareas periódicas, esta escala con el tamaño TOTAL del catálogo, no
    # con lo que cambió -- por eso tiene cola y worker propios
    # (compare_reconcile) para no competir con la sync incremental, y
    # "expires" para descartar corridas atrasadas en vez de encolarlas en
    # cadena si el catálogo crece y una corrida empieza a tardar más que el
    # intervalo.
    CELERY_BEAT_SCHEDULE["compare-subscribers-frequent"] = {
        "task": "wind.tasks.compare_and_update_subscribers_task",
        "schedule": timedelta(minutes=CeleryConfig.COMPARE_SUBSCRIBERS_MINUTES),
        "options": {
            "queue": _COMPARE_SUBSCRIBERS_QUEUE,
            "soft_time_limit": CeleryConfig.COMPARE_SUBSCRIBERS_LOCK_TIMEOUT,
            "time_limit": CeleryConfig.COMPARE_SUBSCRIBERS_LOCK_TIMEOUT + 60,
            "expires": CeleryConfig.COMPARE_SUBSCRIBERS_MINUTES * 60,
        },
        "kwargs": {"limit": _SYNC_LIMIT},
    }

if CeleryConfig.CLOSURE_RETRY_ENABLED:
    CELERY_BEAT_SCHEDULE["retry-partial-closures"] = {
        "task": "wind.tasks.retry_partial_closures_task",
        "schedule": timedelta(minutes=CeleryConfig.CLOSURE_RETRY_MINUTES),
        "options": {
            "queue": _PIPELINE_QUEUE,
            "expires": CeleryConfig.CLOSURE_RETRY_MINUTES * 60,
        },
    }


# ============================================================================
# CACHÉ REDIS
# ============================================================================
REDIS_CACHE_DB = RedisConfig.CACHE_DB

if 'test' in sys.argv or CacheConfig.USE_LOCMEM:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'wind-default',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': RedisConfig.build_url(db=REDIS_CACHE_DB),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            },
        }
    }

PANACCESS_SESSION_USE_REDIS = PanaccessConfig.session_use_redis(
    celery_eager=CELERY_TASK_ALWAYS_EAGER,
)
PANACCESS_SESSION_TTL_SECONDS = PanaccessConfig.SESSION_TTL_SECONDS

PANACCESS_CIRCUIT_BREAKER_ENABLED = PanaccessConfig.circuit_breaker_enabled(debug=DEBUG)
PANACCESS_CB_FAILURE_THRESHOLD = PanaccessConfig.CB_FAILURE_THRESHOLD
PANACCESS_CB_RECOVERY_SECONDS = PanaccessConfig.CB_RECOVERY_SECONDS


# ============================================================================
# SENTRY
# ============================================================================
if SentryConfig.DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        dsn=SentryConfig.DSN,
        integrations=[DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
        traces_sample_rate=SentryConfig.TRACES_SAMPLE_RATE,
        send_default_pii=False,
        environment=SentryConfig.environment(debug=DEBUG),
    )


# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
        'detailed': {
            'format': '[{asctime}] {levelname} [{name}] {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
        'unicode_safe': {
            '()': 'wind.utils.logging_handlers.UnicodeSafeFilter',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG' if DEBUG else 'INFO',
            '()': 'wind.utils.logging_handlers.SafeConsoleHandler',
            'formatter': 'detailed',
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'django.log',
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
            'delay': True,
        },
        'panaccess_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'panaccess.log',
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
            'delay': True,
        },
        'tasks_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'tasks.log',
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
            'delay': True,
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'errors.log',
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
            'delay': True,
        },
    },
    'root': {
        'handlers': ['console', 'file', 'error_file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['error_file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'wind': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
            'filters': ['unicode_safe'],
        },
        'wind.services.panaccess_singleton': {
            'handlers': ['console', 'file', 'panaccess_file', 'error_file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
            'filters': ['unicode_safe'],
        },
        'wind.utils.panaccess_auth': {
            'handlers': ['console', 'file', 'panaccess_file', 'error_file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
            'filters': ['unicode_safe'],
        },
        'wind.services.panaccess_client': {
            'handlers': ['console', 'file', 'panaccess_file', 'error_file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
            'filters': ['unicode_safe'],
        },
        'wind.apps': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
            'filters': ['unicode_safe'],
        },
        'celery': {
            'handlers': ['console', 'tasks_file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'celery.worker': {
            'handlers': ['console', 'tasks_file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'celery.beat': {
            'handlers': ['console', 'tasks_file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

_SOCIALACCOUNT_PROVIDERS_ALL = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'APP': {
            'client_id': SocialConfig.GOOGLE_CLIENT_ID,
            'secret': SocialConfig.GOOGLE_CLIENT_SECRET,
            'key': '',
        },
    },
    'facebook': {
        'METHOD': 'oauth2',
        'SCOPE': ['email', 'public_profile'],
        'FIELDS': ['id', 'email', 'first_name', 'last_name', 'name'],
        'VERIFIED_EMAIL': False,
        'VERSION': 'v20.0',
        'APP': {
            'client_id': SocialConfig.FACEBOOK_APP_ID,
            'secret': SocialConfig.FACEBOOK_APP_SECRET,
            'key': '',
        },
    },
}

SOCIALACCOUNT_PROVIDERS = {
    name: _SOCIALACCOUNT_PROVIDERS_ALL[name]
    for name in SocialConfig.enabled_providers()
    if name in _SOCIALACCOUNT_PROVIDERS_ALL
}

LOGIN_REDIRECT_URL = '/wind/login-test/'
ACCOUNT_LOGOUT_REDIRECT_URL = '/wind/login-test/'
