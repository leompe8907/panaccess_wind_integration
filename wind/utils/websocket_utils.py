import time
import math
import random
import hashlib
import logging
from django.core.cache import cache
from django.utils import timezone
from appConfig import RedisConfig, TrustedProxyConfig

logger = logging.getLogger('rate_limiting')


def is_valid_app_type(app_type: str) -> bool:
    return app_type in [
        'web', 'lg', 'samsung', 'android', 'androidtv', 'amazon', 'iOS', 'iOStv'
    ]


def _get_header_value(source, header_name):
    """
    Obtiene el valor de un header desde request.META (HTTP) o scope (WebSocket).
    """
    if hasattr(source, 'META'):
        return source.META.get(header_name, '')
    
    elif isinstance(source, dict) and 'headers' in source:
        headers = dict(source.get('headers', []))
        header_key = header_name.lower()
        if header_key.startswith('http_'):
            header_key = header_key[5:]
        header_key = header_key.replace('_', '-')
        
        header_key_bytes = header_key.encode().lower()
        for key, value in headers.items():
            if isinstance(key, bytes) and key.lower() == header_key_bytes:
                if isinstance(value, bytes):
                    return value.decode(errors='ignore')
                return str(value)
        return ''
    return ''


def _build_device_fingerprint_string(headers_dict):
    app_type = headers_dict.get('app_type', '')
    mac_address = headers_dict.get('mac_address', '')

    if app_type in ['lg', 'samsung', 'androidtv', 'amazon', 'iOStv']:
        fingerprint_string = (
            f"{app_type}|{headers_dict.get('tv_serial', '')}|"
            f"{headers_dict.get('tv_model', '')}|{headers_dict.get('firmware_version', '')}|"
            f"{headers_dict.get('device_id', '')}|{mac_address}|"
            f"{headers_dict.get('app_version', '')}|{headers_dict.get('user_agent', '')}"
        )
    elif app_type in ['android', 'iOS']:
        fingerprint_string = (
            f"{app_type}|{headers_dict.get('device_id', '')}|"
            f"{headers_dict.get('build_id', '')}|{headers_dict.get('device_model', '')}|"
            f"{headers_dict.get('os_version', '')}|{mac_address}|"
            f"{headers_dict.get('app_version', '')}|{headers_dict.get('user_agent', '')}"
        )
    else:
        fingerprint_string = (
            f"{headers_dict.get('user_agent', '')}|"
            f"{headers_dict.get('accept_language', '')}|"
            f"{headers_dict.get('accept_encoding', '')}|"
            f"{headers_dict.get('accept', '')}|{app_type}|"
            f"{headers_dict.get('app_version', '')}|{headers_dict.get('device_id', '')}|"
            f"{mac_address}"
        )
    return fingerprint_string


def generate_device_fingerprint(request_or_scope):
    # Antes: si el cliente mandaba su propio header `X-Device-Fingerprint`
    # con forma de hex de 32 caracteres, se aceptaba tal cual sin derivar
    # nada -- cualquiera podía "declarar" el fingerprint que quisiera y
    # saltarse por completo el rate-limit por dispositivo (ver auditoría).
    # Ahora el fingerprint SIEMPRE se deriva server-side de las
    # características de la conexión; el cliente no puede declararlo.
    headers_dict = {
        'user_agent': _get_header_value(request_or_scope, 'HTTP_USER_AGENT'),
        'accept_language': _get_header_value(request_or_scope, 'HTTP_ACCEPT_LANGUAGE'),
        'accept_encoding': _get_header_value(request_or_scope, 'HTTP_ACCEPT_ENCODING'),
        'accept': _get_header_value(request_or_scope, 'HTTP_ACCEPT'),
        'device_id': _get_header_value(request_or_scope, 'HTTP_X_DEVICE_ID'),
        'app_version': _get_header_value(request_or_scope, 'HTTP_X_APP_VERSION'),
        'app_type': _get_header_value(request_or_scope, 'HTTP_X_APP_TYPE'),
        'os_version': _get_header_value(request_or_scope, 'HTTP_X_OS_VERSION'),
        'device_model': _get_header_value(request_or_scope, 'HTTP_X_DEVICE_MODEL'),
        'build_id': _get_header_value(request_or_scope, 'HTTP_X_BUILD_ID'),
        'tv_serial': _get_header_value(request_or_scope, 'HTTP_X_TV_SERIAL'),
        'tv_model': _get_header_value(request_or_scope, 'HTTP_X_TV_MODEL'),
        'firmware_version': _get_header_value(request_or_scope, 'HTTP_X_FIRMWARE_VERSION'),
        'mac_address': _get_header_value(request_or_scope, 'HTTP_X_MAC_ADDRESS'),
    }
    
    fingerprint_string = _build_device_fingerprint_string(headers_dict)
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:32]


def _reserve_atomic_slot(cache_key, max_requests, window_seconds):
    """
    Verifica y reserva 1 unidad de cupo en una sola operación atómica de
    caché (`cache.add` para el primer hit de la ventana, `cache.incr` en
    caso contrario, ambos atómicos en el backend de caché).

    Antes, el chequeo (leer el contador con `cache.get`) y la reserva real
    (incrementarlo, en otro punto del código -- `increment_rate_limit_counter`,
    llamado solo después de que la operación de negocio tuviera éxito) eran
    dos pasos separados por I/O de red intermedio: dos requests concurrentes
    podían leer el mismo valor por debajo del límite antes de que cualquiera
    incrementara, dejando pasar más tráfico del configurado (revisión
    adversarial). Cambio de comportamiento aceptado a propósito: ahora el
    cupo se reserva en el momento del chequeo, así que un intento que
    después falla por otra razón (validación, etc.) también cuenta contra
    el límite -- más conservador que antes, nunca menos.
    """
    # Reintento acotado (no un loop sin límite): si la clave expira justo
    # entre el `add` fallido y el `incr` de una misma vuelta, se reintenta
    # desde el `cache.add` -- que es atómico -- en vez de usar `cache.set`
    # a ciegas, que aceptaría a dos requests concurrentes como "la
    # primera" en ese mismo instante y perdería un incremento justo en el
    # borde de expiración de la ventana (revisión adversarial sobre el
    # propio fix).
    for _ in range(2):
        added = cache.add(cache_key, 1, timeout=window_seconds)
        if added:
            return True, max(0, max_requests - 1), 0

        try:
            current = cache.incr(cache_key)
        except ValueError:
            continue

        if current > max_requests:
            # No se "devuelve" el incremento -- se resta de vuelta para
            # que el contador no siga creciendo sin límite mientras dure
            # la ventana (mismo criterio que ya usa check_websocket_limits
            # al exceder su propio límite).
            try:
                cache.decr(cache_key)
            except Exception:
                pass
            return False, 0, window_seconds

        return True, max(0, max_requests - current), 0

    # Ventana inusualmente inestable (la clave expiró en ambos intentos) --
    # se concede el cupo en vez de bloquear indefinidamente.
    cache.set(cache_key, 1, timeout=window_seconds)
    return True, max(0, max_requests - 1), 0


def check_device_fingerprint_rate_limit(device_fingerprint, max_requests=3, window_minutes=5):
    if not device_fingerprint:
        return False, 0, 0
    cache_key = f"rate_limit:device_fp:{device_fingerprint}"
    return _reserve_atomic_slot(cache_key, max_requests, window_minutes * 60)


def check_udid_rate_limit(udid, max_requests=20, window_minutes=60):
    if not udid:
        return False, 0, 0
    cache_key = f"rate_limit:udid:{udid}"
    return _reserve_atomic_slot(cache_key, max_requests, window_minutes * 60)


def check_temp_token_rate_limit(temp_token, max_requests=10, window_minutes=5):
    if not temp_token:
        return False, 0, 0
    cache_key = f"rate_limit:temp_token:{temp_token}"
    return _reserve_atomic_slot(cache_key, max_requests, window_minutes * 60)


def check_udid_account_rate_limit(subscriber_code, max_requests=5, window_minutes=15):
    """
    Rate limit por CUENTA autenticada (no por udid individual) -- usado por
    `AssociateUDIDByAccountView` (pareo UDID auto-servicio desde el
    dashboard web, sin `temp_token`; ver
    docs/PAREO_UDID_AUTOSERVICIO_CUENTA_2026-09-02.md).

    `check_udid_rate_limit` limita intentos contra UN udid específico (20/hora
    por udid) -- eso no protege nada acá, porque un atacante logueado en su
    propia cuenta podría probar muchos udid DISTINTOS, cada uno con su propio
    cupo de 20/hora. Este límite es el que realmente importa: topea el total
    de intentos de asociación de ESA cuenta sin importar cuántos udid
    distintos prueba, así que enumerar el espacio de 32 bits del udid
    (secrets.token_hex(4)) queda fuera de alcance incluso con muchas cuentas
    controladas por el mismo atacante (cada una limitada igual).
    """
    if not subscriber_code:
        return False, 0, 0
    cache_key = f"rate_limit:udid_account:{subscriber_code}"
    return _reserve_atomic_slot(cache_key, max_requests, window_minutes * 60)


def check_websocket_limits(udid, device_fingerprint, max_per_token=5, max_global=1000):
    token_identifier = udid or device_fingerprint
    if not token_identifier:
        return True, None, 0

    token_key = f"ws_connections:token:{token_identifier}"
    global_key = "ws_connections:global"
    redis_client = None
    incremented_token = False
    incremented_global = False

    try:
        redis_client = RedisConfig.get_client()

        token_count = redis_client.incr(token_key)
        incremented_token = True
        if token_count == 1:
            redis_client.expire(token_key, 300)

        if token_count > max_per_token:
            redis_client.decr(token_key)
            return False, "Too many connections for this token", 60

        global_count = redis_client.incr(global_key)
        incremented_global = True
        if global_count == 1:
            redis_client.expire(global_key, 300)

        if global_count > max_global:
            redis_client.decr(global_key)
            redis_client.decr(token_key)
            return False, "Too many global WebSocket connections", 60

        return True, None, 0

    except Exception as e:
        logger.error(f"Error checking WebSocket limits: {e}", exc_info=True)
        # Si algún incremento ya se aplicó en Redis antes de que otra
        # operación de esta misma llamada fallara (p.ej. el `.expire()` o
        # el segundo `.incr()`, por una desconexión intermitente), se
        # revierte -- sin esto, la conexión rechazada nunca pasaría por
        # `decrement_websocket_limits()` (no llega a aceptarse), y fallos
        # transitorios repetidos de Redis podrían ir inflando el contador
        # de forma artificial, rechazando conexiones legítimas incluso
        # después de que Redis se recupere, hasta que la clave expire
        # (revisión adversarial sobre el propio fix de fail-closed).
        if redis_client is not None:
            try:
                if incremented_global:
                    redis_client.decr(global_key)
            except Exception:
                pass
            try:
                if incremented_token:
                    redis_client.decr(token_key)
            except Exception:
                pass
        # Antes: fail-open (dejaba pasar la conexión sin límite si Redis no
        # respondía) -- es una superficie de evasión real: quien logre
        # saturar o tumbar Redis externamente se salta el límite por
        # completo (revisión adversarial). Ahora falla cerrado: sin poder
        # verificar el cupo, no se acepta la conexión nueva. Costo
        # aceptado: si Redis cae, ningún WS nuevo de pareo/dispositivo se
        # acepta hasta que Redis vuelva (las conexiones ya abiertas no se
        # ven afectadas).
        return False, "Servicio de límites temporalmente no disponible", 30


def decrement_websocket_limits(udid, device_fingerprint):
    try:
        redis_client = RedisConfig.get_client()
        token_identifier = udid or device_fingerprint
        if token_identifier:
            token_key = f"ws_connections:token:{token_identifier}"
            try:
                current = redis_client.get(token_key)
                if current and int(current) > 0:
                    redis_client.decr(token_key)
            except Exception:
                pass
        
        global_key = "ws_connections:global"
        try:
            current = redis_client.get(global_key)
            if current and int(current) > 0:
                redis_client.decr(global_key)
        except Exception:
            pass
            
    except Exception as e:
        logger.error(f"Error decrementing WebSocket limits: {e}", exc_info=True)


def check_token_bucket_lua(identifier, capacity=10, refill_rate=1, window_seconds=60, tokens_requested=1):
    """
    Verificación + reserva de rate limit usando contador estándar en Cache
    como alternativa portable a scripts Lua.

    Antes: leía el contador (`cache.get`) y, si había cupo, lo reescribía
    con `cache.set(current + tokens_requested)` -- dos operaciones
    separadas por I/O de red, a pesar del nombre/comentario que sugería
    una verificación atómica tipo Lua. Bajo concurrencia alta, dos
    llamadas simultáneas podían leer el mismo `current` antes de que
    cualquiera escribiera el nuevo valor, y ambas pasar aunque juntas
    superen `capacity` (revisión adversarial) -- exactamente el caso que
    usa `device_consumers.py` para el límite de 20 dispositivos nuevos por
    hora, y el que usan varios endpoints de UDID en `views.py`. Ahora usa
    `cache.add`/`cache.incr` (atómicos), igual que `_reserve_atomic_slot`.
    """
    cache_key = f"rate_limit:tb:{identifier}"

    # Mismo criterio de reintento acotado que `_reserve_atomic_slot`: si la
    # clave expira justo entre el `add` fallido y el `incr`, se reintenta
    # desde el `add` (atómico) en vez de `cache.set` a ciegas, que podría
    # perder un incremento si dos requests caen ahí al mismo tiempo.
    for _ in range(2):
        added = cache.add(cache_key, tokens_requested, timeout=window_seconds)
        if added:
            return True, max(0, capacity - tokens_requested), 0

        try:
            current = cache.incr(cache_key, tokens_requested)
        except ValueError:
            continue

        if current > capacity:
            try:
                cache.decr(cache_key, tokens_requested)
            except Exception:
                pass
            return False, 0, window_seconds

        return True, max(0, capacity - current), 0

    cache.set(cache_key, tokens_requested, timeout=window_seconds)
    return True, max(0, capacity - tokens_requested), 0


def increment_rate_limit_counter(identifier_type, identifier):
    """
    @deprecated -- `check_device_fingerprint_rate_limit`/`check_udid_rate_limit`/
    `check_temp_token_rate_limit` ya reservan el cupo de forma atómica en el
    momento del chequeo (ver `_reserve_atomic_slot`); llamar a esta función
    después, como se hacía antes, incrementaría el contador dos veces por
    request. No queda ningún call-site en este repo -- se deja definida
    solo por si algo externo todavía la importa.
    """
    cache_key = f"rate_limit:{identifier_type}:{identifier}"
    try:
        cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=3600)


def get_client_token(request):
    token = request.META.get('HTTP_X_CLIENT_TOKEN')
    if not token:
        token = request.query_params.get('udid') or request.META.get('HTTP_X_UDID')
    return token


def is_legitimate_reconnection(udid):
    from wind.models import UDIDAuthRequest
    if not udid:
        return False
    
    try:
        req = UDIDAuthRequest.objects.get(udid=udid)
        if req.status in ['validated', 'used']:
            return True
        elif req.status == 'pending' and req.is_expired():
            time_since_expiry = timezone.now() - req.expires_at
            if time_since_expiry.total_seconds() < 3600:
                return True
    except UDIDAuthRequest.DoesNotExist:
        pass
    return False


def get_system_load():
    return 'normal'


def check_circuit_breaker():
    return False, 0


def track_system_request():
    pass


def check_adaptive_rate_limit(identifier_type, identifier, is_reconnection=False, 
                              base_max_requests=None, base_window_minutes=None):
    if base_max_requests is None:
        if identifier_type == 'udid':
            base_max_requests = 5
            base_window_minutes = 60
        elif identifier_type == 'device_fp':
            base_max_requests = 2
            base_window_minutes = 10
        else:
            base_max_requests = 3
            base_window_minutes = 5
            
    if base_window_minutes is None:
        base_window_minutes = 5
        
    if is_reconnection:
        max_requests = base_max_requests * 2
        window_minutes = base_window_minutes
    else:
        max_requests = base_max_requests
        window_minutes = base_window_minutes

    cache_key = f"rate_limit:{identifier_type}:{identifier}"
    current_count = cache.get(cache_key, 0)
    
    if current_count >= max_requests:
        retry_after = window_minutes * 60
        return False, 0, retry_after, "Rate limit exceeded"
        
    return True, max_requests - current_count, 0, "OK"


def calculate_retry_delay(attempt_number, base_delay=1, max_delay=60, jitter=True):
    exponential_delay = base_delay * (2 ** (attempt_number - 1))
    delay = min(exponential_delay, max_delay)
    if jitter:
        jitter_amount = delay * 0.3
        delay = delay + random.uniform(-jitter_amount, jitter_amount)
        delay = max(0.5, delay)
    return int(math.ceil(delay))


def get_retry_info(udid, action_type='reconnection'):
    if not udid:
        return 0, 1
    
    cache_key = f"retry_info:{action_type}:{udid}"
    retry_data = cache.get(cache_key)
    
    if retry_data is None:
        retry_data = {'attempts': 0, 'last_attempt': 0}
        
    attempts = retry_data.get('attempts', 0)
    last_attempt = retry_data.get('last_attempt', 0)
    current_time = time.time()
    
    if current_time - last_attempt > 300:
        attempts = 0
        
    if attempts == 0:
        delay = 0
    else:
        if action_type == 'reconnection':
            delay = calculate_retry_delay(attempts, base_delay=1, max_delay=30, jitter=True)
        else:
            delay = calculate_retry_delay(attempts, base_delay=2, max_delay=60, jitter=True)
            
    attempts += 1
    retry_data['attempts'] = attempts
    retry_data['last_attempt'] = current_time
    cache.set(cache_key, retry_data, timeout=600)
    
    return delay, attempts


def reset_retry_info(udid, action_type='reconnection'):
    if not udid:
        return
    cache_key = f"retry_info:{action_type}:{udid}"
    cache.delete(cache_key)


def should_apply_retry_delay(udid, action_type='reconnection', system_load=None):
    if not udid:
        return False, 0, 0
    
    if system_load is None:
        system_load = get_system_load()
        
    retry_delay, attempt_number = get_retry_info(udid, action_type)
    if retry_delay > 0:
        return True, retry_delay, attempt_number
    return False, 0, attempt_number


def get_client_ip(request):
    """
    IP real del cliente. Antes confiaba en `X-Forwarded-For` sin verificar
    que la request viniera de verdad de un proxy conocido -- cualquier
    cliente podía mandar su propio `X-Forwarded-For` y falsificar la IP que
    termina guardada en `UDIDAuthRequest`/`AuthAuditLog`/
    `EncryptedCredentialsLog` (ver auditoría; mismo patrón de fondo que
    `sync_admin_ip_middleware._client_ip()`, que sigue pendiente de una
    decisión de deploy más amplia -- acá se corrige para esta superficie
    con `TrustedProxyConfig`).

    Solo se confía en `X-Forwarded-For` si la conexión llegó realmente
    desde un proxy conocido (`REMOTE_ADDR` en la allowlist); si no, se usa
    `REMOTE_ADDR` tal cual y el header se ignora por completo.
    """
    remote_addr = request.META.get('REMOTE_ADDR')
    if remote_addr not in TrustedProxyConfig.TRUSTED_PROXIES:
        return remote_addr

    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return remote_addr


def get_client_ip_from_scope(scope):
    """
    Mismo criterio que `get_client_ip()` (HTTP) pero para el scope ASGI de
    un consumer de WebSocket -- antes, `consumers.py`/`device_consumers.py`
    tomaban directo `scope["client"][0]` (el peer inmediato de la conexión
    TCP) sin pasar por ningún filtro de proxy confiable. Si el despliegue
    real corre detrás de un proxy/load balancer, eso guarda la IP del
    proxy (a menudo `127.0.0.1` o la IP interna del LB), no la del
    cliente real, en `UDIDAuthRequest`/`DeviceSession`/`AuthAuditLog`
    (revisión adversarial -- mismo patrón de fondo que ya se corrigió para
    HTTP). Solo se confía en `X-Forwarded-For` si la conexión llegó
    realmente desde un proxy conocido (`TrustedProxyConfig.TRUSTED_PROXIES`);
    si no, se usa el peer directo tal cual y el header se ignora por completo.
    """
    client = scope.get("client") or [None]
    remote_addr = client[0] if client else None

    if remote_addr not in TrustedProxyConfig.TRUSTED_PROXIES:
        return remote_addr or ""

    headers = dict(scope.get("headers", []))
    xff = headers.get(b"x-forwarded-for")
    if xff:
        try:
            return xff.decode(errors="ignore").split(",")[0].strip()
        except Exception:
            pass
    return remote_addr or ""
