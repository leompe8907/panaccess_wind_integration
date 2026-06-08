import os
import time
import math
import random
import hashlib
import logging
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from appConfig import RedisConfig

logger = logging.getLogger('rate_limiting')


def is_valid_app_type(app_type: str) -> bool:
    return app_type in [
        'android_tv', 'samsung_tv', 'lg_tv', 'set_top_box', 'mobile_app', 'web_player'
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
    
    if app_type in ['android_tv', 'samsung_tv', 'lg_tv', 'set_top_box']:
        fingerprint_string = (
            f"{app_type}|{headers_dict.get('tv_serial', '')}|"
            f"{headers_dict.get('tv_model', '')}|{headers_dict.get('firmware_version', '')}|"
            f"{headers_dict.get('device_id', '')}|{mac_address}|"
            f"{headers_dict.get('app_version', '')}|{headers_dict.get('user_agent', '')}"
        )
    elif app_type in ['android_mobile', 'ios_mobile', 'mobile_app']:
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
    direct_fingerprint = _get_header_value(request_or_scope, 'HTTP_X_DEVICE_FINGERPRINT')
    if direct_fingerprint and len(direct_fingerprint) == 32:
        try:
            int(direct_fingerprint, 16)
            return direct_fingerprint
        except ValueError:
            pass
    
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


def check_device_fingerprint_rate_limit(device_fingerprint, max_requests=3, window_minutes=5):
    if not device_fingerprint:
        return False, 0, 0
    
    cache_key = f"rate_limit:device_fp:{device_fingerprint}"
    cached_count = cache.get(cache_key)
    
    if cached_count is not None:
        remaining = max(0, max_requests - cached_count)
        if cached_count >= max_requests:
            retry_after = window_minutes * 60
            return False, remaining, retry_after
        return True, remaining, 0
    
    cache.set(cache_key, 1, timeout=window_minutes * 60)
    return True, max_requests, 0


def check_udid_rate_limit(udid, max_requests=20, window_minutes=60):
    if not udid:
        return False, 0, 0
    
    cache_key = f"rate_limit:udid:{udid}"
    cached_count = cache.get(cache_key)
    
    if cached_count is not None:
        remaining = max(0, max_requests - cached_count)
        if cached_count >= max_requests:
            retry_after = window_minutes * 60
            return False, remaining, retry_after
        return True, remaining, 0
    
    cache.set(cache_key, 1, timeout=window_minutes * 60)
    return True, max_requests, 0


def check_temp_token_rate_limit(temp_token, max_requests=10, window_minutes=5):
    if not temp_token:
        return False, 0, 0
    
    cache_key = f"rate_limit:temp_token:{temp_token}"
    cached_count = cache.get(cache_key)
    
    if cached_count is not None:
        remaining = max(0, max_requests - cached_count)
        if cached_count >= max_requests:
            retry_after = window_minutes * 60
            return False, remaining, retry_after
        return True, remaining, 0
    
    cache.set(cache_key, 1, timeout=window_minutes * 60)
    return True, max_requests, 0


def check_websocket_rate_limit(udid, device_fingerprint, max_connections=5, window_minutes=5):
    if not device_fingerprint:
        return True, 1, 0
    
    cache_key_fp = f"ws_rate_limit:fp:{device_fingerprint}"
    current_connections_fp = cache.get(cache_key_fp, 0)
    
    if current_connections_fp >= max_connections:
        retry_after = window_minutes * 60
        return False, 0, retry_after
    
    if udid:
        cache_key_udid = f"ws_rate_limit:udid:{udid}"
        current_connections_udid = cache.get(cache_key_udid, 0)
        
        if current_connections_udid >= max_connections:
            retry_after = window_minutes * 60
            return False, 0, retry_after
    
    remaining = max_connections - max(current_connections_fp, current_connections_udid if udid else 0)
    return True, remaining, 0


def increment_websocket_connection(udid, device_fingerprint, window_minutes=5):
    timeout = window_minutes * 60
    
    cache_key_fp = f"ws_rate_limit:fp:{device_fingerprint}"
    try:
        cache.incr(cache_key_fp)
    except ValueError:
        cache.set(cache_key_fp, 1, timeout=timeout)
    else:
        cache.expire(cache_key_fp, timeout)
    
    if udid:
        cache_key_udid = f"ws_rate_limit:udid:{udid}"
        try:
            cache.incr(cache_key_udid)
        except ValueError:
            cache.set(cache_key_udid, 1, timeout=timeout)
        else:
            cache.expire(cache_key_udid, timeout)


def decrement_websocket_connection(udid, device_fingerprint):
    cache_key_fp = f"ws_rate_limit:fp:{device_fingerprint}"
    try:
        current = cache.get(cache_key_fp, 0)
        if current > 0:
            cache.set(cache_key_fp, current - 1)
    except Exception:
        pass
    
    if udid:
        cache_key_udid = f"ws_rate_limit:udid:{udid}"
        try:
            current = cache.get(cache_key_udid, 0)
            if current > 0:
                cache.set(cache_key_udid, current - 1)
        except Exception:
            pass


def check_websocket_limits(udid, device_fingerprint, max_per_token=5, max_global=1000):
    try:
        redis_client = RedisConfig.get_client()
        token_identifier = udid or device_fingerprint
        if not token_identifier:
            return True, None, 0
        
        token_key = f"ws_connections:token:{token_identifier}"
        token_count = redis_client.incr(token_key)
        if token_count == 1:
            redis_client.expire(token_key, 300)
        
        if token_count > max_per_token:
            redis_client.decr(token_key)
            return False, "Too many connections for this token", 60
        
        global_key = "ws_connections:global"
        global_count = redis_client.incr(global_key)
        if global_count == 1:
            redis_client.expire(global_key, 300)
        
        if global_count > max_global:
            redis_client.decr(global_key)
            redis_client.decr(token_key)
            return False, "Too many global WebSocket connections", 60
        
        return True, None, 0
        
    except Exception as e:
        logger.error(f"Error checking WebSocket limits: {e}", exc_info=True)
        return True, None, 0


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
    Verificación de rate limit usando contador estándar en Cache
    como alternativa portable y segura a scripts Lua.
    """
    cache_key = f"rate_limit:tb:{identifier}"
    current = cache.get(cache_key)
    if current is not None:
        if current >= capacity:
            return False, 0, window_seconds
        cache.set(cache_key, current + tokens_requested, timeout=window_seconds)
        return True, capacity - (current + tokens_requested), 0
    
    cache.set(cache_key, tokens_requested, timeout=window_seconds)
    return True, capacity - tokens_requested, 0


def increment_rate_limit_counter(identifier_type, identifier):
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
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.META.get('REMOTE_ADDR')
