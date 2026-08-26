"""
Autenticación de abonados: credenciales PanAccess (login1/login2/código) y usuarios Django.
"""
import hmac
import logging

from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache

from appConfig import AuthLockoutConfig, PanaccessConfig

from wind.functions.getSubscriber import CallListExtendedSubscribers
from wind.functions.getSubscriberLoginInfo import fetch_login_info_for_subscriber
from wind.models import (
    ListOfSubscriber,
    SubscriberEmailRegistry,
    SubscriberLoginInfo,
)
from wind.utils.encryption import decrypt_value

logger = logging.getLogger(__name__)
User = get_user_model()


def _check_password_hash(password_hash: str | None, raw_password: str) -> bool:
    if not password_hash or not raw_password:
        return False
    try:
        # hmac.compare_digest en vez de == -- comparación de tiempo
        # constante, no corta apenas encuentra la primera diferencia (ver
        # auditoría).
        return hmac.compare_digest(decrypt_value(password_hash), raw_password)
    except Exception:
        return False


def find_login_record(login: str) -> SubscriberLoginInfo | None:
    """Busca credenciales PanAccess en BD local por login1, login2 o código."""
    login = (login or "").strip()
    if not login:
        return None

    if login.isdigit():
        record = SubscriberLoginInfo.objects.filter(login1=int(login)).first()
        if record:
            return record

    record = SubscriberLoginInfo.objects.filter(login2__iexact=login).first()
    if record:
        return record

    record = SubscriberLoginInfo.objects.filter(subscriberCode=login).first()
    if record:
        return record

    if login.isdigit():
        # Registro manual (ajuste de subscriber_code): el código ahora se
        # guarda como "BM$<documento>", pero el usuario puede seguir
        # tecleando solo su documento por costumbre -- se intenta también con
        # el prefijo antes de rendirse.
        from wind.functions.create_subscriber import MANUAL_CODE_PREFIX

        record = SubscriberLoginInfo.objects.filter(
            subscriberCode=f"{MANUAL_CODE_PREFIX}{login}"
        ).first()
        if record:
            return record

    return None


def resolve_subscriber_code(login: str) -> str | None:
    """Resuelve código de suscriptor a partir de texto libre (código, email, etc.)."""
    login = (login or "").strip()
    if not login:
        return None

    sub = ListOfSubscriber.objects.filter(code=login).first()
    if not sub:
        sub = ListOfSubscriber.objects.filter(code__iexact=login).first()
    if not sub and login.isdigit():
        # Mismo fallback que find_login_record(): el código de registro
        # manual ahora lleva el prefijo "BM$", el usuario sigue tecleando
        # solo su documento.
        from wind.functions.create_subscriber import MANUAL_CODE_PREFIX

        sub = ListOfSubscriber.objects.filter(code=f"{MANUAL_CODE_PREFIX}{login}").first()
    if sub and sub.code:
        return sub.code

    if "@" in login:
        reg = SubscriberEmailRegistry.objects.filter(email__iexact=login).first()
        if reg and reg.subscriber_code:
            return reg.subscriber_code
        sub = ListOfSubscriber.objects.filter(emails__iexact=login).first()
        if sub and sub.code:
            return sub.code

    return None


def fetch_and_find_login_record(login: str) -> SubscriberLoginInfo | None:
    """Intenta traer credenciales desde PanAccess si conocemos el código de suscriptor."""
    code = resolve_subscriber_code(login)
    if code:
        try:
            fetch_login_info_for_subscriber(subscriber_code=code)
        except Exception as exc:
            logger.warning("No se pudo obtener login info de PanAccess para %s: %s", code, exc)

        record = SubscriberLoginInfo.objects.filter(subscriberCode=code).first()
        if record:
            return record

    return find_login_record(login)


_DISCOVERY_MISS_CACHE_PREFIX = "wind:login1_discovery_miss:"


def _discover_login_by_login1(login_int: int, password: str) -> SubscriberLoginInfo | None:
    """
    Busca en PanAccess el suscriptor cuyo login1 coincide (cuando no está en BD local).
    Limitado por PANACCESS_LOGIN_DISCOVERY_MAX_CALLS para no saturar la API.

    Fase 2 (Alto #5, ver docs/OPTIMIZACION_LATENCIA_LOGIN_2026-08-26.md): si
    un descubrimiento termina sin encontrar nada (login1 inexistente, o
    existente pero con contraseña incorrecta), se recuerda el "miss" en
    caché un rato corto para no repetir hasta LOGIN_DISCOVERY_MAX_CALLS
    llamadas reales a PanAccess en cada reintento contra el mismo número.
    Si el suscriptor SÍ existe y la contraseña coincide, `try_codes` ya deja
    el registro guardado localmente (fetch_login_info_for_subscriber), así
    que un reintento correcto inmediatamente después nunca vuelve a llegar
    hasta acá -- lo intercepta `find_login_record()` en
    `verify_panaccess_credentials()`. No se cachea la contraseña, solo el
    hecho de que la búsqueda no dio resultado.
    """
    max_calls = PanaccessConfig.LOGIN_DISCOVERY_MAX_CALLS
    if max_calls <= 0:
        return None

    miss_cache_key = f"{_DISCOVERY_MISS_CACHE_PREFIX}{login_int}"
    if cache.get(miss_cache_key):
        return None

    calls = 0

    def try_codes(codes):
        nonlocal calls
        for code in codes:
            if not code or calls >= max_calls:
                return None
            calls += 1
            try:
                fetch_login_info_for_subscriber(subscriber_code=code)
            except Exception:
                continue
            record = SubscriberLoginInfo.objects.filter(login1=login_int).first()
            if record and record.check_password(password):
                return record
        return None

    def run_discovery():
        local_codes = ListOfSubscriber.objects.exclude(code="").values_list("code", flat=True)
        found = try_codes(local_codes)
        if found:
            return found

        offset = 0
        page_size = 50
        while calls < max_calls:
            try:
                answer = CallListExtendedSubscribers(offset=offset, limit=page_size)
            except Exception as exc:
                logger.warning("Descubrimiento login1: error listando suscriptores: %s", exc)
                break

            rows = answer.get("extendedSubscriberEntries") or answer.get("rows") or []
            if not rows:
                break

            for row in rows:
                unique_login = row.get("uniqueLogin")
                if unique_login is not None and int(unique_login) == login_int:
                    code = row.get("subscriberCode") or row.get("code")
                    if code:
                        found = try_codes([code])
                        if found:
                            return found

            codes = [
                row.get("subscriberCode") or row.get("code")
                for row in rows
                if row.get("subscriberCode") or row.get("code")
            ]
            found = try_codes(codes)
            if found:
                return found

            if len(rows) < page_size:
                break
            offset += page_size

        return None

    result = run_discovery()
    if result is None and PanaccessConfig.LOGIN_DISCOVERY_MISS_CACHE_SECONDS > 0:
        cache.set(miss_cache_key, True, timeout=PanaccessConfig.LOGIN_DISCOVERY_MISS_CACHE_SECONDS)
    return result


def verify_panaccess_credentials(login: str, password: str) -> SubscriberLoginInfo | None:
    record = find_login_record(login)
    if record and record.check_password(password):
        return record

    record = fetch_and_find_login_record(login)
    if record and record.check_password(password):
        return record

    if login.isdigit():
        return _discover_login_by_login1(int(login), password)

    return None


def _resolve_email_for_subscriber(subscriber_code: str) -> str:
    reg = SubscriberEmailRegistry.objects.filter(subscriber_code=subscriber_code).first()
    if reg and reg.email:
        return reg.email

    sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    if sub and sub.emails:
        return sub.emails.strip().lower()

    return f"{subscriber_code}@subscribers.wind.local"


def mark_portal_email_verified(user: User, email: str) -> None:
    """
    Marca el email como verificado en allauth.

    Los abonados registrados vía PanAccess ya validaron el contacto (validateContactOfSubscriber).
    Sin esto, ACCOUNT_EMAIL_VERIFICATION=mandatory bloquea el login del portal web.
    """
    email = (email or "").strip().lower()
    if not email:
        return

    try:
        from allauth.account.models import EmailAddress
    except ImportError:
        return

    email_address, _ = EmailAddress.objects.get_or_create(
        user=user,
        email=email,
        defaults={"primary": True, "verified": True},
    )
    updated_fields: list[str] = []
    if not email_address.verified:
        email_address.verified = True
        updated_fields.append("verified")
    if not email_address.primary:
        EmailAddress.objects.filter(user=user, primary=True).exclude(
            pk=email_address.pk
        ).update(primary=False)
        email_address.primary = True
        updated_fields.append("primary")
    if updated_fields:
        email_address.save(update_fields=updated_fields)


def ensure_subscriber_portal_email_verified(
    user: User, login: str = "", *, subscriber_code: str | None = None
) -> None:
    """
    Marca el email verificado si el usuario está vinculado a un abonado PanAccess.

    `subscriber_code`: si el caller ya resolvió el código (ej.
    `authenticate_portal_user`, que ya llama `resolve_subscriber_code(login)`
    un poco antes), se pasa acá para no repetir la misma resolución dos
    veces en el mismo request -- optimización de latencia de login (Alto
    #5, ver docs/OPTIMIZACION_LATENCIA_LOGIN_2026-08-26.md). Si no se pasa,
    se resuelve igual que antes (compatibilidad con otros callers).
    """
    email = (user.email or login or "").strip().lower()
    if not email:
        return

    is_subscriber = (
        SubscriberEmailRegistry.objects.filter(email__iexact=email).exists()
        or bool(subscriber_code if subscriber_code is not None else resolve_subscriber_code(login or email))
    )
    if is_subscriber:
        mark_portal_email_verified(user, email)


def is_subscriber_closed_locally(subscriber_code: str | None) -> bool:
    """
    True si el abonado está CLOSED o PENDING_CLOSURE en la tabla local.

    Usado para no dejar entrar (ni reactivar el usuario del portal) a una
    cuenta que ya cerramos localmente, sin importar si PanAccess todavía
    acepta esas credenciales (auditoría, sección 17/21: el login nunca
    revisaba este estado, así que cerrar la cuenta no impedía volver a
    entrar si las credenciales seguían siendo válidas del lado de
    PanAccess -- confirmado en la práctica por el cliente).
    """
    if not subscriber_code:
        return False
    # Fuerza lectura a primaria (no réplica): esta es la puerta que decide
    # si un login se rechaza por cierre de cuenta. Si leyera de una réplica
    # con lag, un cierre recién escrito en primaria podría no verse todavía
    # acá, dejando una ventana real para loguearse en una cuenta ya cerrada
    # -- justo el bypass que este chequeo existe para evitar (ver
    # wind/db_router.py).
    from wind.db_router import use_primary_for_reads

    with use_primary_for_reads():
        sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    if not sub:
        return False
    return sub.status in (ListOfSubscriber.STATUS_CLOSED, ListOfSubscriber.STATUS_PENDING_CLOSURE)


def get_or_create_portal_user(login_record: SubscriberLoginInfo) -> User:
    """Crea o actualiza un User de Django vinculado al abonado PanAccess."""
    code = login_record.subscriberCode or ""
    email = _resolve_email_for_subscriber(code)
    username = str(login_record.login1) if login_record.login1 else (login_record.login2 or code)

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        user = User.objects.filter(username=username).first()

    if not user:
        user = User.objects.create_user(
            username=username,
            email=email,
            password=None,
        )
    elif user.email != email:
        user.email = email

    raw_password = login_record.get_password()
    if raw_password:
        user.set_password(raw_password)
    # No reactivar una cuenta que cerramos localmente -- ver
    # is_subscriber_closed_locally(). El caller (authenticate_portal_user)
    # ya debería haber bloqueado el login antes de llegar acá; esto es una
    # segunda capa para cualquier otro caller presente o futuro.
    if not is_subscriber_closed_locally(code):
        user.is_active = True
    user.save()
    mark_portal_email_verified(user, email)
    return user


_LOGIN_LOCKOUT_PREFIX = "wind:login_lockout:"
_LOGIN_FAILCOUNT_PREFIX = "wind:login_failcount:"


def _lockout_identifier(login: str) -> str:
    return (login or "").strip().lower()


def _is_login_locked(identifier: str) -> bool:
    if not identifier:
        return False
    return bool(cache.get(f"{_LOGIN_LOCKOUT_PREFIX}{identifier}"))


def _register_failed_login(identifier: str) -> None:
    """Suma un intento fallido y bloquea temporalmente si se supera el umbral."""
    if not identifier:
        return
    key = f"{_LOGIN_FAILCOUNT_PREFIX}{identifier}"
    try:
        attempts = cache.incr(key)
    except ValueError:
        # La clave no existía o expiró -- arranca el contador con TTL de la
        # ventana de conteo (no de bloqueo).
        cache.set(key, 1, timeout=AuthLockoutConfig.WINDOW_SECONDS)
        attempts = 1

    if attempts >= AuthLockoutConfig.MAX_ATTEMPTS:
        cache.set(
            f"{_LOGIN_LOCKOUT_PREFIX}{identifier}",
            True,
            timeout=AuthLockoutConfig.LOCKOUT_SECONDS,
        )
        cache.delete(key)
        logger.warning(
            "Login bloqueado temporalmente tras %s intentos fallidos: %s",
            attempts,
            identifier,
        )


def _clear_failed_logins(identifier: str) -> None:
    if not identifier:
        return
    cache.delete(f"{_LOGIN_FAILCOUNT_PREFIX}{identifier}")
    cache.delete(f"{_LOGIN_LOCKOUT_PREFIX}{identifier}")


def authenticate_portal_user(login: str, password: str):
    """
    Autentica por usuario Django (email/username) o credenciales PanAccess (texto libre).
    Retorna User o None.

    Envoltorio delgado sobre `_authenticate_portal_user_core` que agrega
    bloqueo temporal de cuenta tras varios intentos fallidos seguidos (Alto
    #5, Fase 3 -- ver docs/OPTIMIZACION_LATENCIA_LOGIN_2026-08-26.md).

    El contador y el bloqueo viven en caché (Redis), por identificador de
    login normalizado (texto tal cual lo tipeó el usuario: email, código,
    login1 o login2) -- no en `SubscriberInfo.failed_login_attempts`/
    `locked_until`, que existían pero pertenecen al perfil de
    smartcard/activación y nunca se consultan ni se actualizan en este flujo
    de login (serían un bloqueo sin efecto real). Un login bloqueado no
    intenta ninguna autenticación (ni siquiera contra la BD local),
    respondiendo de inmediato.
    """
    identifier = _lockout_identifier(login)
    if AuthLockoutConfig.ENABLED and _is_login_locked(identifier):
        logger.warning(
            "Login rechazado para %s: bloqueado temporalmente por intentos fallidos",
            login,
        )
        return None

    user = _authenticate_portal_user_core(login, password)

    if AuthLockoutConfig.ENABLED:
        if user:
            _clear_failed_logins(identifier)
        else:
            _register_failed_login(identifier)

    return user


def _authenticate_portal_user_core(login: str, password: str):
    """
    Lógica real de autenticación (antes el cuerpo de `authenticate_portal_user`).

    Nota (auditoría, sección 17/21): el camino por credenciales PanAccess
    (`verify_panaccess_credentials`) puede encontrar la contraseña cacheada
    localmente, o volver a pedirla en vivo a PanAccess si no está en caché
    (`fetch_and_find_login_record`) -- en cualquiera de los dos casos, si
    esa cuenta ya la cerramos localmente (`ListOfSubscriber.status`), no se
    debe conceder acceso ni reactivar el usuario del portal, sin importar
    si PanAccess todavía acepta esas credenciales.
    """
    login = (login or "").strip()
    if not login or not password:
        return None

    user = authenticate(username=login, password=password)
    if user:
        # authenticate() de Django ya respeta is_active, pero si el usuario
        # sigue activo y el abonado vinculado resulta estar cerrado (ej. se
        # cerró la cuenta por otro medio sin desactivar este User todavía),
        # se bloquea igual acá.
        # Se resuelve UNA sola vez y se reutiliza en ensure_subscriber_
        # portal_email_verified() -- antes se resolvía dos veces seguidas
        # con el mismo `login` (Alto #5, optimización de latencia de login).
        subscriber_code = resolve_subscriber_code(login)
        if is_subscriber_closed_locally(subscriber_code):
            logger.warning("Login rechazado para %s: abonado cerrado localmente", login)
            return None
        user.backend = getattr(user, "backend", "django.contrib.auth.backends.ModelBackend")
        ensure_subscriber_portal_email_verified(user, login, subscriber_code=subscriber_code)
        return user

    if "@" in login:
        by_email = User.objects.filter(email__iexact=login).first()
        if by_email:
            user = authenticate(username=by_email.get_username(), password=password)
            if user:
                subscriber_code = resolve_subscriber_code(login)
                if is_subscriber_closed_locally(subscriber_code):
                    logger.warning("Login rechazado para %s: abonado cerrado localmente", login)
                    return None
                user.backend = getattr(user, "backend", "django.contrib.auth.backends.ModelBackend")
                ensure_subscriber_portal_email_verified(user, login, subscriber_code=subscriber_code)
                return user

    login_record = verify_panaccess_credentials(login, password)
    if login_record:
        if is_subscriber_closed_locally(login_record.subscriberCode):
            logger.warning(
                "Login rechazado para %s: abonado %s cerrado localmente "
                "(PanAccess todavía aceptó las credenciales)",
                login,
                login_record.subscriberCode,
            )
            return None
        user = get_or_create_portal_user(login_record)
        user.backend = "django.contrib.auth.backends.ModelBackend"
        # subscriber_code ya se conoce (login_record.subscriberCode) -- se
        # evita una tercera resolución redundante de resolve_subscriber_code()
        # en el mismo request (Alto #5, optimización de latencia de login).
        ensure_subscriber_portal_email_verified(
            user, login, subscriber_code=login_record.subscriberCode
        )
        return user

    return None
